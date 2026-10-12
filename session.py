import xml.etree.ElementTree as etree
import re
from liveplan import session_steps


class Session:
    def __init__(self, session=None):
        root = etree.parse(session).getroot()
        self.name = root.attrib['name']
        self.duration = self.parse_timestring(root.attrib['duration'])
        
        for timeevent in root.find('timeEvents'):

            action = timeevent.find('action').attrib['type']
            value = float(timeevent.find('action').attrib['value'])

            time = timeevent.attrib['time']
            if time[0] == '+':
                for t in range(0, self.parse_timestring(time.split('+')[1])-1):
                    prevstep = self.steps[-1:][0]
                    self.steps.append({
                            'time': prevstep['time']+1,
                            'power': prevstep['power'],
                            'pulse': prevstep['pulse'],
                            'recovery': prevstep['recovery']
                            })

                prevstep = self.steps[-1:][0]
                self.steps.append({
                        'time': prevstep['time']+1,
                        'power': value if action in ('power', 'recovery') else 0,
                        'recovery': action == 'recovery',
                        'pulse': value if action == 'pulse' else 0
                        })
            
            elif time == '0':
                self.steps = []
                self.steps.append({
                        'time': 0,
                        'power': value if action in ('power', 'recovery') else 0,
                        'recovery': action == 'recovery',
                        'pulse': value if action == 'pulse' else 0
                        })
        
        for i in range(0, self.duration - len(self.steps)):
            prevstep = self.steps[-1]
            self.steps.append({
                    'time': prevstep['time']+1,
                    'power': prevstep['power'],
                    'pulse': prevstep['pulse'],
                    'recovery': prevstep['recovery']
                    })
    
    def parse_timestring(self, s):
        duration = 0
        for component in re.findall(r'(\d*(h|m|s))', s):
            if component[1] == 'h':
                duration += int(component[0].split('h')[0]) * 3600
            if component[1] == 'm':
                duration += int(component[0].split('m')[0]) * 60
            if component[1] == 's':
                duration += int(component[0].split('s')[0])
        
        return duration

    def step(self, second):
        try:
            s = self.steps[second]
        except:
            s = self.steps[-1]
        return s

    def update_live_plan(self, plan):
        steps = session_steps(plan)
        self.steps = steps
        self.duration = len(steps)
