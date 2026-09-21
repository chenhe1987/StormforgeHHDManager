import win32evtlog
import time

class EventLogMonitor:
    def __init__(self):
        self.server = 'localhost'
        self.log_type = 'System'
        self.last_check_time = time.time()
        # Common event sources for disk/filesystem issues
        self.sources = ['Disk', 'Ntfs', 'ReFS', 'storahci', 'uas']

    def check_new_errors(self):
        errors = []
        try:
            hand = win32evtlog.OpenEventLog(self.server, self.log_type)
            flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
            
            while True:
                events = win32evtlog.ReadEventLog(hand, flags, 0)
                if not events:
                    break
                
                for event in events:
                    # If event is older than our last check, stop (since we read backwards)
                    event_time = event.TimeGenerated.timestamp()
                    if event_time <= self.last_check_time:
                        # Optimization: we can stop here if we are sure no more new events
                        # but win32evtlog backwards read can be tricky, so we continue for a bit
                        # or just break if we are sure.
                        break
                    
                    # Level 1 = Error, Level 2 = Warning
                    if event.EventType in [win32evtlog.EVENTLOG_ERROR_TYPE, win32evtlog.EVENTLOG_WARNING_TYPE]:
                        if event.SourceName in self.sources:
                            errors.append({
                                "time": event.TimeGenerated.Format(),
                                "source": event.SourceName,
                                "id": event.EventID & 0xFFFF,
                                "message": event.StringInserts if event.StringInserts else "No details"
                            })
                else:
                    continue # only if inner loop didn't break
                break # break outer loop if inner loop broke
                
            win32evtlog.CloseEventLog(hand)
        except Exception as e:
            print(f"Error reading event log: {e}")
            
        self.last_check_time = time.time()
        return errors

if __name__ == "__main__":
    # Diagnostic monitor
    monitor = EventLogMonitor()
    print("Checking for disk errors in System Log (last 24h)...")
    monitor.last_check_time = time.time() - 86400
    new_errors = monitor.check_new_errors()
    for err in new_errors:
        print(f"[{err['time']}] {err['source']} Error({err['id']}): {err['message']}")
