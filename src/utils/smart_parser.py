import struct
import time
import datetime

class SmartAttribute:
    def __init__(self, attr_id, name, name_cn, value, worst, raw):
        self.id = attr_id
        self.name = name
        self.name_cn = name_cn
        self.value = value
        self.worst = worst
        self.raw = raw

    def __repr__(self):
        return f"ID: {self.id:02X}, Name: {self.name_cn}({self.name}), Value: {self.value}, Worst: {self.worst}, Raw: {self.raw}"

class SmartParser:
    # Common SMART Attribute Names with Chinese translation
    ATTRIBUTE_INFO = {
        0x01: ("Read Error Rate", "底层数据读取错误率"),
        0x03: ("Spin Up Time", "主轴起旋时间"),
        0x04: ("Start/Stop Count", "启停次数"),
        0x05: ("Reallocated Sectors Count", "重定向扇区计数"),
        0x07: ("Seek Error Rate", "寻道错误率"),
        0x09: ("Power-On Hours", "累计通电时间"),
        0x0A: ("Spin Retry Count", "主轴起旋重试次数"),
        0x0C: ("Power Cycle Count", "通电次数"),
        0xB8: ("End-to-End Error", "端到端错误"),
        0xBB: ("Reported Uncorrectable Errors", "报告的不可修复错误"),
        0xBC: ("Command Timeout", "指令超时"),
        0xBD: ("High Fly Writes", "磁头飞行高度监视"),
        0xBE: ("Airflow Temperature", "气流温度"),
        0xBF: ("G-Sense Error Rate", "冲击力检测错误率"),
        0xC0: ("Power-off Retract Count (Unsafe Shutdown Count)", "不安全关机数 (0xC0)"),
        0xC1: ("Load Cycle Count", "磁头加载/卸载循环计数"),
        0xC2: ("Temperature", "温度"),
        0xC3: ("Hardware ECC Recovered", "硬件 ECC 恢复"),
        0xC4: ("Reallocation Event Count", "重定向事件计数"),
        0xC5: ("Current Pending Sector Count", "当前待处理扇区数"),
        0xC6: ("Offline Uncorrectable", "脱机不可修复扇区数"),
        0xC7: ("UDMA CRC Error Count", "UDMA CRC 错误计数"),
        0xF0: ("Head Flying Hours", "磁头飞行时间"),
        0xF1: ("Total LBAs Written", "累计写入量 (LBA)"),
        0xF2: ("Total LBAs Read", "累计读取量 (LBA)"),
        # Common SSD Attributes
        0xA7: ("SSD Protect Mode", "SSD 保护模式"),
        0xA8: ("SATA PHY Error Count", "SATA 物理层错误计数"),
        0xA9: ("Bad Block Count", "坏块计数"),
        0xAD: ("Wear Leveling Count", "磨损平衡计数"),
        0xAF: ("Program Fail Count (Chip)", "编程失败计数 (芯片)"),
        0xB0: ("Erase Fail Count (Chip)", "擦除失败计数 (芯片)"),
        0xB1: ("Wear Range Delta", "磨损范围增量"),
        0xB2: ("Used Reserved Block Count", "已用保留块计数"),
        0xB3: ("Used Reserved Block Count Total", "总已用保留块计数"),
        0xB4: ("Unused Reserved Block Count Total", "总未使用保留块计数"),
        0xBB: ("Uncorrectable Error Count", "不可校正错误计数"),
        0xE7: ("SSD Life Left", "SSD 剩余寿命"),
        0xE8: ("Available Reserved Space", "可用保留空间"),
        0xE9: ("Media Wearout Indicator", "介质磨损指标"),
        0xF3: ("Total LBAs Written Expanded", "累计写入量 (扩展)"),
        0xF4: ("Total LBAs Read Expanded", "累计读取量 (扩展)"),
    }

    # Knowledge Base: (Severity, Interpretation, Troubleshooting)
    # Severity: 0=Info, 1=Warning, 2=Critical
    KNOWLEDGE_BASE = {
        0x01: (0, "磁盘从盘片读取数据时发生的错误率。", "如果此数值持续大幅增加，可能预示磁头或盘片表面有问题。建议备份数据。"),
        0x03: (0, "主轴电机达到额定转速所需的时间。", "通常无需关注。如果数值变大，可能电源供电不足或电机老化。"),
        0x04: (0, "主轴电机启动/停止的次数。", "机械硬盘的寿命指标之一。"),
        0x05: (2, "因坏道而重新映射到保留区的扇区数量。", "严重警告：物理坏道正在产生。请立即备份重要数据并准备更换硬盘。"),
        0x07: (0, "磁头寻道时的错误率。", "偶尔增加可能是震动导致。如果持续快速增加，可能是机械部件即将故障。"),
        0x09: (0, "硬盘累计通电运行的小时数。", "用于评估硬盘服役年限。"),
        0x0A: (1, "电机启动失败并尝试重新启动的次数。", "警告：可能预示电机故障或电源供电不稳。请检查电源线接口。"),
        0x0C: (0, "硬盘通电/断电的完整周期次数。", "参考指标。"),
        0xBB: (2, "无法通过硬件ECC校正的错误。", "严重警告：数据完整性可能受损。建议立即备份。"),
        0xBC: (1, "因超时而未完成的操作计数。", "警告：可能由数据线接触不良或电源问题引起。请更换数据线测试。"),
        0xC2: (0, "硬盘内部当前温度。", "长期超过 55°C 会缩短寿命。请改善机箱散热。"),
        0xC4: (1, "重映射操作发生的次数。", "警告：与 05 属性类似，预示有坏道产生。"),
        0xC5: (2, "等待被重新映射的不稳定扇区。", "严重警告：高风险区域。如果写入数据失败，将变成物理坏道。建议全盘扫描或更换。"),
        0xC6: (2, "脱机扫描时发现的无法修复扇区。", "严重警告：盘片表面存在物理损伤。"),
        0xC7: (1, "接口通信校验错误次数。", "警告：通常是 SATA 数据线故障或接口接触不良。请更换高质量数据线。"),
        0xA7: (1, "SSD 处于写保护模式。", "警告：SSD 可能已达到寿命极限，变为只读状态。请尽快复制数据。"),
        0xA9: (2, "出厂后产生的坏块数量。", "严重警告：NAND 颗粒可能开始损坏。"),
        0xAD: (0, "磨损平衡操作计数。", "SSD 正常损耗指标。"),
        0xE7: (1, "SSD 剩余寿命百分比。", "当低于 10% 时请注意数据安全。"),
    }

    @staticmethod
    def parse_identify_device(data):
        """Parse IDENTIFY DEVICE data (512 bytes) to get model, serial, firmware"""
        if len(data) < 512:
            return {}
        
        def get_string(offset, length):
            try:
                # Words are big-endian swapped in ATA string fields
                s = bytearray(data[offset : offset + length])
                # Swap pairs
                for i in range(0, length, 2):
                    s[i], s[i+1] = s[i+1], s[i]
                return s.decode('ascii', errors='ignore').strip()
            except:
                return ""

        # Serial Number: words 10-19 (offset 20, length 20)
        serial = get_string(20, 20)
        # Firmware Revision: words 23-26 (offset 46, length 8)
        firmware = get_string(46, 8)
        # Model Number: words 27-46 (offset 54, length 40)
        model = get_string(54, 40)
        
        return {
            "serial": serial,
            "firmware": firmware,
            "model": model
        }

    @staticmethod
    def parse_512(data):
        if len(data) < 512:
            return []

        attributes = []
        for i in range(30):
            offset = 2 + (i * 12)
            attr_data = data[offset : offset + 12]
            
            attr_id = attr_data[0]
            if attr_id == 0:
                continue
                
            value = attr_data[3]
            worst = attr_data[4]
            raw_bytes = attr_data[5:11]
            raw_value = int.from_bytes(raw_bytes, byteorder='little')
            
            # Special handling for Power-On Hours (ID 9)
            # Some drives (e.g. Western Digital) store extra data in high bytes
            if attr_id == 9 and raw_value > 500000: # > 57 years
                # Try masking to lower 32 bits (standard 4-byte integer)
                lower_32 = raw_value & 0xFFFFFFFF
                # If the lower 32 bits result in a reasonable value (< 57 years), use it
                if lower_32 < 500000:
                    raw_value = lower_32
            
            info = SmartParser.ATTRIBUTE_INFO.get(attr_id, (f"Unknown (0x{attr_id:02X})", "未知属性"))
            name, name_cn = info
            
            attributes.append(SmartAttribute(attr_id, name, name_cn, value, worst, raw_value))
            
        return attributes

    @staticmethod
    def parse_nvme(data):
        """Parse NVMe Health Information Log (512 bytes)"""
        if len(data) < 512:
            return []
            
        attributes = []
        
        # NVMe Health Log is fixed format, not ID-Value pairs like SATA.
        # We simulate some attributes to fit into the UI.
        
        critical_warning = data[0]
        temp_k = int.from_bytes(data[1:3], byteorder='little') # Temperature in Kelvin
        temp_c = temp_k - 273 if temp_k > 0 else 0
        spare = data[3]
        percentage_used = data[5]
        data_units_read = int.from_bytes(data[32:48], byteorder='little') * 1000 * 512 # Convert to bytes
        data_units_written = int.from_bytes(data[48:64], byteorder='little') * 1000 * 512
        power_cycles = int.from_bytes(data[80:96], byteorder='little')
        power_on_hours = int.from_bytes(data[96:112], byteorder='little')
        
        # Add simulated attributes
        attributes.append(SmartAttribute(0x01, "Critical Warning", "严重警告", 0, 0, critical_warning))
        attributes.append(SmartAttribute(0x02, "Composite Temperature", "综合温度", 0, 0, temp_c))
        attributes.append(SmartAttribute(0x03, "Available Spare", "可用备用空间", 0, 0, spare))
        attributes.append(SmartAttribute(0x05, "Percentage Used", "已用寿命百分比", 0, 0, percentage_used))
        attributes.append(SmartAttribute(0x09, "Power On Hours", "通电时间", 0, 0, power_on_hours))
        attributes.append(SmartAttribute(0x0C, "Power Cycles", "通电次数", 0, 0, power_cycles))
        attributes.append(SmartAttribute(0xF1, "Data Units Written", "累计写入量 (GB)", 0, 0, data_units_written // (1024**3)))
        attributes.append(SmartAttribute(0xF2, "Data Units Read", "累计读取量 (GB)", 0, 0, data_units_read // (1024**3)))
        
        return attributes

    @staticmethod
    def parse_powershell_nvme(data):
        """
        Parse data from PowerShell Get-StorageReliabilityCounter.
        Data keys: DeviceId, Temperature, Wear, PowerOnHours, ReadErrorsTotal, WriteErrorsTotal
        """
        attributes = []
        
        # 1. Critical Warning (Simulated)
        attributes.append(SmartAttribute(1, 'Critical Warning', '严重警告', 100, 100, 0))
        
        # 2. Temperature
        temp = data.get('Temperature')
        if temp is None: 
            temp = 0
        else:
            try: temp = int(float(temp))
            except: temp = 0
        attributes.append(SmartAttribute(2, 'Temperature', '温度', 100, 100, temp))
        
        # 3. Available Spare (Simulate from Wear?)
        wear = data.get('Wear')
        if wear is None: 
            wear = 0
        else:
            try: wear = int(float(wear))
            except: wear = 0
        spare = 100 - wear
        attributes.append(SmartAttribute(3, 'Available Spare', '可用备用空间', spare, spare, spare))
        
        # 4. Percentage Used
        attributes.append(SmartAttribute(5, 'Percentage Used', '已用寿命百分比', wear, wear, wear))
        
        # 6. Power On Hours
        hours = data.get('PowerOnHours')
        if hours is not None:
             attributes.append(SmartAttribute(9, 'Power On Hours', '通电时间', 100, 100, int(hours)))
             
        # Read/Write Errors
        read_err = data.get('ReadErrorsTotal')
        if read_err is not None:
            attributes.append(SmartAttribute(0xC9, 'Read Errors Total', '读取错误总数', 100, 100, int(read_err)))
            
        write_err = data.get('WriteErrorsTotal')
        if write_err is not None:
            attributes.append(SmartAttribute(0xCA, 'Write Errors Total', '写入错误总数', 100, 100, int(write_err)))
            
        return attributes

    @staticmethod
    def get_summary(attributes, history_entry=None):
        summary = {
            "temp": None,
            "reallocated": None, # Use None to indicate missing
            "pending": None,
            "crc_errors": None,
            "power_on_hours": 0,
            "power_cycle_count": 0,
            "status": "Healthy",
            "health_advice": "",
            # SSD specific
            "ssd_life_left": None,
            "total_writes_gb": None,
            "total_reads_gb": None,
            "attributes_analysis": {}, # New: Per-attribute analysis
            "critical_warning": None,
            "percent_used": None
        }
        
        # Helper to get previous raw value for an attribute ID
        def get_prev_raw(attr_id):
            if not history_entry: return None
            # history_entry structure: {"first_seen": ts, "last_check": ts, "attributes": {id: raw}}
            attrs = history_entry.get("attributes", {})
            val = attrs.get(str(attr_id))
            return int(val) if val is not None else None

        # Helper to format duration
        def format_duration(seconds):
            if seconds < 60:
                return "刚刚"
            elif seconds < 3600:
                return f"{int(seconds // 60)}分钟"
            elif seconds < 86400:
                return f"{int(seconds // 3600)}小时"
            else:
                return f"{int(seconds // 86400)}天"

        # Calculate monitoring duration
        monitoring_duration_str = "本次运行期间"
        if history_entry and "first_seen" in history_entry:
             first_seen = history_entry["first_seen"]
             duration_sec = time.time() - first_seen
             monitoring_duration_str = f"在过去 {format_duration(duration_sec)} 的监控中"

        for attr in attributes:
            # 1. Store raw values for history
            # (Logic moved to MonitorService or upper layer, but we process current values here)
            
            # 2. Extract Key Metrics (Existing logic)
            if attr.id == 0xC2 or attr.id == 0xBE or attr.id == 0x02: # Temperature
                summary["temp"] = attr.raw & 0xFFFF
            elif attr.id == 0x01: # Read Error Rate (SATA) or Critical Warning (NVMe simulated)
                if attr.name == "Critical Warning":
                    summary["critical_warning"] = attr.raw
                else:
                    summary["read_error_rate"] = attr.raw
            elif attr.id == 0x05: # Reallocated (SATA) or Percentage Used (NVMe simulated)
                if attr.name == "Percentage Used":
                    summary["percent_used"] = attr.raw
                else:
                    summary["reallocated"] = attr.raw
            elif attr.id == 0xC5: # Pending
                summary["pending"] = attr.raw
            elif attr.id == 0xC7: # CRC Errors
                summary["crc_errors"] = attr.raw
            elif attr.id == 0x09: # Power-On Hours
                summary["power_on_hours"] = attr.raw
            elif attr.id == 0x0C: # Power Cycle Count
                summary["power_cycle_count"] = attr.raw
            elif attr.id == 0xE7: # SSD Life Left
                summary["ssd_life_left"] = attr.raw
            elif attr.id == 0xF1: # Total Writes
                # Check unit? usually LBA or GB depending on parser
                if "GB" in attr.name_cn:
                    summary["total_writes_gb"] = attr.raw
                else:
                    # Assume LBA (512 bytes) -> GB
                    summary["total_writes_gb"] = attr.raw * 512 // (1024**3)
            elif attr.id == 0xF2: # Total Reads
                 if "GB" in attr.name_cn:
                    summary["total_reads_gb"] = attr.raw
                 else:
                    summary["total_reads_gb"] = attr.raw * 512 // (1024**3)

            # 3. Dynamic Analysis for EVERY attribute
            prev_val = get_prev_raw(attr.id)
            analysis = {
                "delta": 0,
                "interpretation": "",
                "troubleshoot": ""
            }
            
            # Lookup knowledge base
            kb = SmartParser.KNOWLEDGE_BASE.get(attr.id)
            
            if prev_val is not None:
                delta = attr.raw - int(prev_val)
                analysis["delta"] = delta
                
                if kb:
                    severity, base_interp, base_trouble = kb
                    current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                    
                    if delta > 0:
                        # Increased!
                        if severity > 0:
                            analysis["interpretation"] = (
                                f"警告：在 {current_time_str} 的检测中发现数值增加了 {delta} (当前: {attr.raw}, 上次: {prev_val})。\n"
                                f"{base_interp} 数值增加是不正常的。\n"
                                f"可能原因：硬盘老化、物理损伤或连接不稳定。"
                            )
                            analysis["troubleshoot"] = f"{base_trouble}\n建议：该指标属于累积性错误，近期增加表明问题正在发生，请密切关注。"
                        else:
                            # Normal increase (like hours)
                            analysis["interpretation"] = f"数值正常累积 (新增 {delta})。{base_interp}"
                            analysis["troubleshoot"] = "无需操作。"
                    else:
                        # No increase
                        if attr.raw > 0 and severity > 0:
                            # High value but stable
                            analysis["interpretation"] = (
                                f"检测到当前数值为 {attr.raw}。\n"
                                f"请检查这是否是历史数据。{monitoring_duration_str}，这个数据并未增加 (增量为 0)，说明这很可能是历史遗留问题。\n"
                                f"虽然当前状态稳定，但 {base_interp}"
                            )
                            analysis["troubleshoot"] = f"{base_trouble}\n建议：只要数值不继续增加，通常无需恐慌，但建议定期备份。"
                        else:
                            # Normal value (0) or Info attribute
                            analysis["interpretation"] = base_interp
                            analysis["troubleshoot"] = base_trouble
            else:
                 # No history yet
                 if kb:
                    severity, base_interp, base_trouble = kb
                    if attr.raw > 0 and severity > 0:
                         analysis["interpretation"] = (
                             f"当前数值为 {attr.raw}。这是首次监测到该数据，暂时无法判断增长趋势。\n"
                             f"{base_interp}"
                         )
                         analysis["troubleshoot"] = f"{base_trouble}\n建议：保持软件运行以积累历史数据，观察该数值是否会增加。"
                    else:
                        analysis["interpretation"] = base_interp
                        analysis["troubleshoot"] = base_trouble
            
            summary["attributes_analysis"][attr.id] = analysis


        # Generate intelligent summary and advice
        advices = []
        status = "Healthy"
        
        # 1. Critical Warning (NVMe)
        if summary.get("critical_warning") is not None and summary["critical_warning"] > 0:
            status = "Critical"
            cw = summary["critical_warning"]
            reasons = []
            if cw & 0x01: reasons.append("备用空间过低")
            if cw & 0x02: reasons.append("温度异常")
            if cw & 0x04: reasons.append("可靠性下降 (NAND 错误)")
            if cw & 0x08: reasons.append("介质设为只读")
            if cw & 0x10: reasons.append("易失性存储器备份失败")
            advices.append(f"严重警告：NVMe 硬盘报告关键性错误 (代码 0x{cw:02X}: {', '.join(reasons)})！请立即备份并更换硬盘。")

        # 2. Power on analysis
        hours = summary["power_on_hours"]
        years = hours / 8760
        power_str = f"硬盘已累计通电 {hours} 小时"
        if years > 5:
            advices.append(f"硬盘已服役超过 {years:.1f} 年，建议加强数据备份。")
        elif hours < 100:
            advices.append("这是一块较新的硬盘，请关注初期运行状态。")
            
        # 3. Health risk analysis
        if summary["reallocated"] is not None and summary["reallocated"] > 0:
            count = summary["reallocated"]
            prev_val = get_prev_raw(0x05) # ID for Reallocated
            delta = count - prev_val if prev_val is not None else 0
                
            status = "Warning"
            if delta > 0:
                 advices.append(f"严重警告：发现新增的重映射扇区 (新增 {delta} 个，总计 {count} 个)！\n硬盘物理状况正在恶化，请立即备份数据并更换硬盘。")
            else:
                 advices.append(f"发现 {count} 个历史重映射扇区。数值暂未增加，建议持续密切观察。")
            
        if summary["pending"] is not None and summary["pending"] > 0:
            count = summary["pending"]
            prev_val = get_prev_raw(0xC5) # ID for Pending
            delta = count - prev_val if prev_val is not None else 0

            status = "Warning"
            if delta > 0:
                advices.append(f"高风险：待处理扇区正在增加 (新增 {delta} 个，总计 {count} 个)。\n这些扇区可能很快转化为永久坏道，请备份数据。")
            else:
                advices.append(f"存在 {count} 个待处理扇区。数值暂未增加，但仍属于高风险状态，请定期检查。")

        if summary.get("percent_used") is not None and summary["percent_used"] > 90:
            status = "Warning"
            advices.append(f"NVMe 寿命已消耗 {summary['percent_used']}%，接近设计寿命终点，建议近期更换。")
            
        if summary["ssd_life_left"] is not None and summary["ssd_life_left"] < 10:
             status = "Warning"
             advices.append(f"SSD 剩余寿命仅剩 {summary['ssd_life_left']}%，请务必及时备份数据。")

        if summary["crc_errors"] is not None and summary["crc_errors"] > 0:
            count = summary["crc_errors"]
            prev_val = get_prev_raw(0xC7) # ID for CRC Errors
            delta = count - prev_val if prev_val is not None else 0
            
            if delta > 0:
                status = "Warning"
                advices.append(f"注意！UDMA CRC 错误正在增加 (新增 {delta} 次，总计 {count} 次)。\n这表明当前数据传输不稳定，请立即检查或更换 SATA 数据线。")
            elif count > 50:
                # High count but stable
                advices.append(f"检测到历史累计的 UDMA CRC 错误 ({count} 次)。\n近期数值未增加，说明通信已稳定，可能是历史遗留问题。")

        # 4. Temperature analysis
        temp = summary["temp"]
        if temp and temp > 55:
            advices.append(f"当前温度 ({temp}°C) 偏高，建议改善散热环境，长期高温会缩短硬盘寿命。")

        if not advices:
            advices.append("硬盘各项指标正常，目前运行状态良好。建议保持良好的散热和稳定的供电。")

        summary["status"] = status
        summary["health_advice"] = "\n".join(advices)
        summary["power_info"] = power_str
        
        return summary
