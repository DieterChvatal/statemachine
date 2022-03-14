import pandas as pd
import numpy as np
import copy
from pprint import pformat as pf
import arrow
from tqdm.auto import tqdm
import dmyplant2
import logging
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

class StateVector:
    statechange = False
    laststate = ''
    laststate_start = None,
    currentstate = ''
    currentstate_start = None
    in_operation = ''
    service_selector = ''
    msg = None

# States und Transferfunktionen, Sammeln von Statebezogenen Daten ... 
class State:
    def __init__(self, statename, transferfun_list):
        self._statename = statename
        self._transferfunctions = transferfun_list
        self._trigger = False
    
    def send(self,msg):
        for transfun in self._transferfunctions: # screen triggers
            self._trigger = msg['name'] == transfun['trigger'][:4]
            if self._trigger:
                return transfun['new-state']
        return self._statename

    def update_vector(self, vector):
        vector.laststate = self._statename
        vector.laststate_start = vector.currentstate_start
        vector.currentstate_start = pd.to_datetime(vector.msg['timestamp'] * 1e6)
        return vector        

    def trigger_on_vector(self, vector):
        vector.currentstate = self.send(vector.msg)
        vector.statechange = self._trigger
        if self._trigger:
            vector = self.update_vector(vector)
        return [vector]

# SpezialFall Loadram, hier wird ein berechneter Statechange ermittelt.
class LoadrampState(State):
    def __init__(self, statename, transferfun_list, e):
        self._e = e
        self._full_load_timestamp = None
        self._loadramp = self._e['rP_Ramp_Set'] or 0.625 # %/sec
        self._default_ramp_duration = 100.0 / self._loadramp
        super().__init__(statename, transferfun_list)

    def trigger_on_vector(self, vector):
        retsv = super().trigger_on_vector(vector)
        vector = retsv[0]
        if vector.msg['name'] == '9047' and self._full_load_timestamp != None:
            self._full_load_timestamp = vector.msg['timestamp']
        if self._full_load_timestamp != None and int(vector.msg['timestamp']) >= self._full_load_timestamp: # now switch to 'targetoperation'
                vector1 = self.update_vector(vector)
                vector1.currentstate = 'targetoperation'

                vector2 = copy.deepcopy(vector1) # copy vector, insert the calculated state_vector
                vector2.msg = {'name':'9047', 'message':'Target load reached (calculated)','timestamp':self._full_load_timestamp,'severity':600}
                vector2.statechange = True
                vector2.currentstate = 'targetoperation'
                vector2.currentstate_start = pd.to_datetime(self._full_load_timestamp * 1e6)
                self._full_load_timestamp = None
                return [vector2,vector1]
        if self._full_load_timestamp == None:
            self._full_load_timestamp = int(vector.msg['timestamp']) + self._default_ramp_duration * 1e3
        return [vector]


# dataClass FSM
class operationFSM:
    def __init__(self, e):
        self._e = e
        self._initial_state = 'standstill'
        self._states = {
                'standstill': State('standstill',[
                    { 'trigger':'1231 Request module on', 'new-state': 'startpreparation'},            
                    ]),
                'startpreparation': State('startpreparation',[
                    { 'trigger':'1249 Starter on', 'new-state': 'starter'},
                    { 'trigger':'1232 Request module off', 'new-state': 'standstill'}
                    ]),
                'starter': State('starter',[
                    { 'trigger':'3225 Ignition on', 'new-state':'speedup'},
                    { 'trigger':'1232 Request module off', 'new-state':'standstill'}
                    ]),
                'speedup': State('speedup',[
                    { 'trigger':'2124 Idle', 'new-state':'idle'},
                    { 'trigger':'3226 Ignition off', 'new-state':'standstill'}
                    ]),             
                'idle': State('idle',[
                    { 'trigger':'2139 Request Synchronization', 'new-state':'synchronize'},
                    { 'trigger':'3226 Ignition off', 'new-state':'standstill'}
                    ]),
                'synchronize': State('synchronize',[
                    { 'trigger':'1235 Generator CB closed', 'new-state':'loadramp'},                
                    { 'trigger':'3226 Ignition off', 'new-state':'standstill'}
                    ]),             
                'loadramp': LoadrampState('loadramp',[
                    { 'trigger':'3226 Ignition off', 'new-state':'standstill'}
                    ], e),             
                'targetoperation': State('targetoperation',[
                    { 'trigger':'1232 Request module off', 'new-state':'rampdown'},
                    ]),
                'rampdown': State('rampdown',[
                    { 'trigger':'1236 Generator CB opened', 'new-state':'coolrun'},
                    { 'trigger':'1231 Request module on', 'new-state':'targetoperation'},
                    ]),
                'coolrun': State('coolrun',[
                    { 'trigger':'1234 Operation off', 'new-state':'runout'},
                    { 'trigger':'3226 Ignition off', 'new-state':'standstill'}
                    ]),
                'runout': State('runout',[
                    { 'trigger':'3226 Ignition off', 'new-state':'standstill'},
                    { 'trigger':'1231 Request module on', 'new-state': 'startpreparation'},            
                ])
            }

    @property
    def initial_state(self):
        return self._initial_state

    @property
    def states(self):
        return self._states

class filterFSM:
    run2filter_content = ['no','success','mode','startpreparation','starter','speedup','idle','synchronize','loadramp','cumstarttime','maxload','ramprate','targetoperation','rampdown','coolrun','runout','count_alarms', 'count_warnings']
    vertical_lines_times = ['startpreparation','starter','speedup','idle','synchronize','loadramp','targetoperation','rampdown','coolrun','runout']

class demoFSM:
    def __init__(self, e, p_from = None, p_to=None, skip_days=None, frompickle=False, successtime=600):
        self._e = e
        self._successtime = successtime
        self.load_messages(e, p_from, p_to, skip_days)

        fsmStates = operationFSM(self._e)
        self.states = fsmStates.states

        self.svec = StateVector()
        self.svec.statechange = True
        self.svec.laststate = 'init'
        self.svec.laststate_start = self.first_message
        self.svec.currentstate = fsmStates.initial_state
        self.svec.currentstate_start = self.first_message
        self.svec.in_operation = 'off'
        self.svec.service_selector = '???'

        self._runlog = []
        self._runlogdetail = []
        self.init_results()

    def init_results(self):
        self.results = {
            'starts': [],
            'starts_counter':0,
            'stops': [
            {
                'run2':False,
                'no': 0,
                'mode': self.svec.service_selector,
                'starttime': self.svec.laststate_start,
                'endtime': pd.Timestamp(0),
                'alarms':[],
                'warnings':[]                
            }],
            'stops_counter':0,
            'runlog': []
        }     

    @property
    def starts(self):
        return pd.DataFrame(self.results['starts'])

    @property
    def stops(self):
        return pd.DataFrame(self.results['stops'])

    ## message handling
    def load_messages(self,e, p_from=None, p_to=None, skip_days=None):
        self._messages = e.get_messages(p_from, p_to)
        pfrom_ts = int(pd.to_datetime(p_from, infer_datetime_format=True).timestamp() * 1000) if p_from else 0
        pto_ts = int(pd.to_datetime(p_to, infer_datetime_format=True).timestamp() * 1000) if p_to else int(pd.Timestamp.now().timestamp() * 1000)
        self._messages = self._messages[(self._messages.timestamp > pfrom_ts) & (self._messages.timestamp < pto_ts)]
        self.first_message = pd.to_datetime(self._messages.iloc[0]['timestamp']*1e6)
        self.last_message = pd.to_datetime(self._messages.iloc[-1]['timestamp']*1e6)
        self._period = pd.Timedelta(self.last_message - self.first_message).round('S')
        if skip_days and not p_from:
            self.first_message = pd.Timestamp(arrow.get(self.first_message).shift(days=skip_days).timestamp()*1e9)
            self._messages = self._messages[self._messages['timestamp'] > int(arrow.get(self.first_message).shift(days=skip_days).timestamp()*1e3)]
        self.count_messages = self._messages.shape[0]

    def msgtxt(self, msg, idx=0):
        return f"{idx:>06} {msg['severity']} {msg['timestamp']} {pd.to_datetime(int(msg['timestamp'])*1e6).strftime('%d.%m.%Y %H:%M:%S')}  {msg['name']} {msg['message']}"

    def msg_smalltxt(self, msg, idx=0):
        return f"{msg['severity']} {pd.to_datetime(int(msg['timestamp'])*1e6).strftime('%d.%m.%Y %H:%M:%S')}  {msg['name']} {msg['message']}"

    def save_messages(self, fn):
        with open(fn, 'w') as f:
            for index, msg in self._messages.iterrows():
                f.write(self.msgtxt(msg, index)+'\n')
                #f.write(f"{index:>06} {msg['severity']} {msg['timestamp']} {pd.to_datetime(int(msg['timestamp'])*1e6).strftime('%d.%m.%Y %H:%M:%S')}  {msg['name']} {msg['message']}\n")
                if 'associatedValues' in msg:
                    if msg['associatedValues'] == msg['associatedValues']:  # if not NaN ...
                        f.write(f"{pf(msg['associatedValues'])}\n\n")

    def save_runlog(self, fn):
        if len(self._runlog):
            with open(fn, 'w') as f:
                for line in self._runlog:
                    f.write(line + '\n')

#################################################################################################################
### die Finite State Machines:
    def _fsm_Service_selector(self):
        if self.svec.msg['name'] == '1225 Service selector switch Off'[:4]:
            self.svec.service_selector = 'OFF'
        if self.svec.msg['name'] == '1226 Service selector switch Manual'[:4]:
            self.svec.service_selector = 'MANUAL'
        if self.svec.msg['name'] == '1227 Service selector switch Automatic'[:4]:
            self.svec.service_selector = 'AUTO'

    def _fsm_collect_alarms(self):
        key = 'starts' if self.svec.in_operation == 'on' else 'stops'
        if self.svec.msg['severity'] == 800:
            self.results[key][-1]['alarms'].append({
                'state':self.svec.currentstate, 
                'msg': self.svec.msg
                })
        if self.svec.msg['severity'] == 700:
            self.results[key][-1]['warnings'].append({
                'state':self.svec.currentstate, 
                'msg': self.svec.msg
                })

    def _fsm_Operating_Cycle(self):
        if self.svec.statechange:
            if self.svec.currentstate == 'startpreparation':
                self.results['stops'][-1]['endtime'] = self.svec.currentstate_start
                self.results['stops'][-1]['count_alarms'] = len(self.results['stops'][-1]['alarms'])
                self.results['stops'][-1]['count_warnings'] = len(self.results['stops'][-1]['warnings'])
                # apends a new record to the Starts list.
                self.results['starts'].append({
                    'run2':False,
                    'no':self.results['starts_counter'],
                    'success': False,
                    'mode':self.svec.service_selector,
                    'starttime': self.svec.laststate_start,
                    'endtime': pd.Timestamp(0),
                    'cumstarttime': pd.Timedelta(0),
                    'startpreparation':np.nan,
                    'starter':np.nan,
                    'speedup':np.nan,
                    'idle':np.nan,
                    'synchronize':np.nan,
                    'loadramp':np.nan,
                    'targetoperation':np.nan,
                    'rampdown':np.nan,
                    'coolrun':np.nan,
                    'runout':np.nan,
                    'timing': {},
                    'alarms': [],
                    'warnings': [],
                    'maxload': np.nan,
                    'ramprate': np.nan
                })
                self.results['starts_counter'] += 1 # index for next start
                self.svec.in_operation = 'on'
            elif self.svec.in_operation == 'on': # and actstate != FSM.initial_state:            
                self.results['starts'][-1]['timing']['start_'+ self.svec.laststate] = self.svec.laststate_start 
                self.results['starts'][-1]['timing']['end_'+ self.svec.laststate] = self.svec.currentstate_start 

            if self.svec.currentstate == 'standstill':
                if self.svec.in_operation == 'on':
                    # start finished
                    self.results['starts'][-1]['endtime'] = self.svec.currentstate_start
                    # calc phase durations
                    phases = [x[6:] for x in self.results['starts'][-1]['timing'] if x.startswith('start_')]
                    durations = { ph:pd.Timedelta(self.results['starts'][-1]['timing']['end_'+ph] - self.results['starts'][-1]['timing']['start_'+ph]).total_seconds() for ph in phases}
                    durations['cumstarttime'] = sum([v for k,v in durations.items() if k in ['startpreparation','starter','speedup','idle','synchronize','loadramp']])
                    self.results['starts'][-1].update(durations)
                    if 'targetoperation' in self.results['starts'][-1]:
                        #successful if the targetoperation run was longer than specified
                        self.results['starts'][-1]['success'] = (self.results['starts'][-1]['targetoperation'] > self._successtime)
                    self.results['starts'][-1]['count_alarms'] = len(self.results['starts'][-1]['alarms'])
                    self.results['starts'][-1]['count_warnings'] = len(self.results['starts'][-1]['warnings'])
 
                self.svec.in_operation = 'off'
                self.results['stops_counter'] += 1 # index for next start
                self.results['stops'].append({
                    'run2':False,
                    'no': self.results['stops_counter'],
                    'mode': self.svec.service_selector,
                    'starttime': self.svec.laststate_start,
                    'endtime': pd.Timestamp(0),
                    'alarms':[],
                    'warnings':[]
                })

            _logline= {
                'laststate': self.svec.laststate,
                'laststate_start': self.svec.laststate_start,
                'msg': self.svec.msg['name'] + ' ' + self.svec.msg['message'],
                'currenstate': self.svec.currentstate,
                'currentstate_start': self.svec.currentstate_start,
                'starts': len(self.results['starts']),
                'Successful_starts': len([s for s in self.results['starts'] if s['success']]),
                'operation': self.svec.in_operation,
                'mode': self.svec.service_selector,
            }
            self.results['runlog'].append(_logline)

    def call_trigger_states(self):
        return self.states[self.svec.currentstate].trigger_on_vector(self.svec)

    ## FSM Entry Point.
    def run1(self, enforce=False):
        if len(self.results['starts']) == 0 or enforce or not ('run2' in self.results['starts'][0]):
            self.init_results()     
            #for i,msg in tqdm(self._messages.iterrows(), total=self._messages.shape[0], ncols=80, mininterval=1, unit=' messages', desc="FSM"):
            for i, msg in self._messages.iterrows():

                self.svec.msg = msg
                retsv = self.call_trigger_states()
                for sv in retsv:   
                    self.svec = sv   
                    self._runlogdetail.append(
                        f"= {'*' if self.svec.statechange else '':2} {self.svec.laststate:18} " + \
                        f"{self.svec.laststate_start.strftime('%d.%m %H:%M:%S')} -> " + \
                        f"{self.svec.currentstate:18}" + \
                        f"{self.svec.currentstate_start.strftime('%d.%m %H:%M:%S')} " + \
                        f"{self.msg_smalltxt(self.svec.msg)}")

                    self._fsm_Service_selector()
                    self._fsm_collect_alarms()
                    self._fsm_Operating_Cycle()

#########################################################################################################################
    def dorun2(self, index_list, startversuch):
                ii = startversuch['no']
                index_list.append(ii)

                if not startversuch['run2']:

                    data = dmyplant2.get_cycle_data2(self, startversuch, max_length=None, min_length=None, silent=True)

                    if not data.empty:

                        pl, _ = dmyplant2.detect_edge_left(data, 'Power_PowerAct', startversuch)
                        #pr, _ = detect_edge_right(data, 'Power_PowerAct', startversuch)
                        #sl, _ = detect_edge_left(data, 'Various_Values_SpeedAct', startversuch)
                        #sr, _ = detect_edge_right(data, 'Various_Values_SpeedAct', startversuch)

                        self._starts[ii]['title'] = f"{self._e} ----- Start {ii} {startversuch['mode']} | {'SUCCESS' if startversuch['success'] else 'FAILED'} | {startversuch['starttime'].round('S')}"
                        #sv_lines = {k:(startversuch[k] if k in startversuch else np.NaN) for k in filterFSM.vertical_lines_times]}
                        sv_lines = [v for v in startversuch[filterFSM.vertical_lines_times]]
                        start = startversuch['starttime'];
                        
                        # lade die in run1 gesammelten Daten in ein DataFrame, ersetze NaN Werte mit 0
                        backup = {}
                        svdf = pd.DataFrame(sv_lines, index=filterFSM.vertical_lines_times, columns=['FSM'], dtype=np.float64).fillna(0)
                        svdf['RUN2'] = svdf['FSM']

                        # intentionally excluded - Dieter 1.3.2022
                        #if svdf.at['speedup','FSM'] > 0.0:
                        #        svdf.at['speedup','RUN2'] = sl.loc.timestamp() - start.timestamp() - np.cumsum(svdf['RUN2'])['starter']
                        #        svdf.at['idle','RUN2'] = svdf.at['idle','FSM'] - (svdf.at['speedup','RUN2'] - svdf.at['speedup','FSM'])
                        if svdf.at['loadramp','FSM'] > 0.0:
                                calc_loadramp = pl.loc.timestamp() - start.timestamp() - np.cumsum(svdf['RUN2'])['synchronize']
                                svdf.at['loadramp','RUN2'] = calc_loadramp

                                # collect run2 results.
                                backup['loadramp'] = svdf.at['loadramp','FSM'] # alten Wert merken
                                self._starts[ii]['loadramp'] = calc_loadramp

                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            calc_maxload = pl.val
                            try:
                                calc_ramp = (calc_maxload / self._e['Power_PowerNominal']) * 100 / svdf.at['loadramp','RUN2']
                            except ZeroDivisionError as err:
                                logging.warning(f"calc_ramp: {str(err)}")
                                calc_ramp = np.NaN
                            # doppelte Hosenträger ... hier könnte man ein wenig aufräumen :-)
                            if not np.isfinite(calc_ramp) :
                                calc_ramp = np.NaN

                            backup_cumstarttime = np.cumsum(svdf['FSM'])['loadramp']
                            calc_cumstarttime = np.cumsum(svdf['RUN2'])['loadramp']
                        svdf = pd.concat([
                                svdf, 
                                pd.DataFrame.from_dict(
                                        {       'maxload':['-',calc_maxload],
                                                'ramprate':['-',calc_ramp],
                                                'cumstarttime':[backup_cumstarttime, calc_cumstarttime]
                                        }, 
                                        columns=['FSM','RUN2'],
                                        orient='index')]
                                )
                        #display(HTML(svdf.round(2).T.to_html(escape=False)))

                        # collect run2 results.
                        self._starts[ii]['maxload'] = calc_maxload
                        self._starts[ii]['ramprate'] = calc_ramp
                        backup['cumstarttime'] = backup_cumstarttime
                        self._starts[ii]['cumstarttime'] = calc_cumstarttime

                        self._starts[ii]['backup'] = backup
                        self._starts[ii]['run2'] = True


    def run2(self, rda, silent=False):
        index_list = []
        if silent:
            for n, startversuch in rda.iterrows():
                self.dorun2(index_list, startversuch)
        else:
            for n, startversuch in tqdm(rda.iterrows(), total=rda.shape[0], ncols=80, mininterval=1, unit=' starts', desc="FSM Run2"):
                self.dorun2(index_list, startversuch)
        return pd.DataFrame([self._starts[s] for s in index_list])
