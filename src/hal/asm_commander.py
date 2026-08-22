import ctypes
import logging
from .win32_api import (
    Win32API, SCSI_PASS_THROUGH_DIRECT, IOCTL_SCSI_PASS_THROUGH_DIRECT, 
    SCSI_IOCTL_DATA_IN, SCSI_IOCTL_DATA_OUT, SCSI_PASS_THROUGH_DIRECT_WITH_SENSE,
    IOCTL_STORAGE_QUERY_PROPERTY, STORAGE_PROPERTY_QUERY, STORAGE_PROTOCOL_SPECIFIC_DATA,
    STORAGE_PROTOCOL_DATA_DESCRIPTOR, ProtocolTypeNvme, NVMeDataTypeLogPage, NVMeLogPageHealthInfo,
    IOCTL_ATA_PASS_THROUGH, ATA_PASS_THROUGH_EX, ATA_PASS_THROUGH_EX_WITH_BUFFER,
    ATA_FLAGS_DRDY_REQUIRED, ATA_FLAGS_DATA_IN
)

class ASMCommander:
    def __init__(self, drive_index, model_hint=None, serial_hint=None, existing_handle=None):
        self.drive_index = drive_index
        self.model_hint = model_hint
        self.serial_hint = serial_hint
        self.handle = None
        self._external_handle = existing_handle  # 复用已打开的句柄（系统弹出场景）

    def __enter__(self):
        if self._external_handle:
            # 系统弹出场景：设备正处于 QUERYREMOVE 阶段，重新打开 PhysicalDriveN
            # 可能失败（设备已锁定，报 1117），直接复用 SafeRemovalPatcher 已注册的句柄。
            self.handle = self._external_handle
            return self
        self.handle = Win32API.open_physical_drive(self.drive_index)
        if not self.handle:
            err = ctypes.get_last_error()
            logging.error(f"DEBUG: Failed to open PhysicalDrive{self.drive_index}. Last error: {err} ({hex(err)})")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.handle and not self._external_handle:
            Win32API.close_handle(self.handle)
            self.handle = None

    def get_nvme_smart_data(self):
        """Read NVMe Health Information Log using IOCTL_STORAGE_QUERY_PROPERTY"""
        if not self.handle:
            return None

        # Output buffer for STORAGE_PROTOCOL_DATA_DESCRIPTOR + 512 bytes data
        output_buffer_size = ctypes.sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR) + 512
        output_buffer = (ctypes.c_ubyte * output_buffer_size)()
        
        # Try PropertyId 28 first (Device), then 30 (Adapter) if it fails
        for prop_id in [28, 30]:
            query = STORAGE_PROPERTY_QUERY()
            query.PropertyId = prop_id
            query.QueryType = 0 # PropertyStandardQuery
            
            # Cast AdditionalParameters to STORAGE_PROTOCOL_SPECIFIC_DATA
            protocol_data = ctypes.cast(query.AdditionalParameters, ctypes.POINTER(STORAGE_PROTOCOL_SPECIFIC_DATA)).contents
            protocol_data.ProtocolType = ProtocolTypeNvme
            protocol_data.DataType = NVMeDataTypeLogPage
            protocol_data.ProtocolDataRequestValue = NVMeLogPageHealthInfo
            protocol_data.ProtocolDataRequestSubValue = 0
            protocol_data.ProtocolDataOffset = ctypes.sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR)
            protocol_data.ProtocolDataLength = 512
            
            result, bytes_returned, error_code = Win32API.device_io_control(
                self.handle,
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(output_buffer),
                output_buffer_size
            )
            
            if result:
                # The log data starts at ProtocolDataOffset in the output buffer
                descriptor = ctypes.cast(output_buffer, ctypes.POINTER(STORAGE_PROTOCOL_DATA_DESCRIPTOR)).contents
                offset = descriptor.ProtocolSpecificData.ProtocolDataOffset
                if offset > 0 and offset < output_buffer_size:
                    log_data = bytes(output_buffer[offset : offset + 512])
                    logging.info(f"Drive {self.drive_index}: NVMe SMART query successful with PropertyId {prop_id}")
                    return log_data
            else:
                logging.debug(f"Drive {self.drive_index}: NVMe SMART query failed with PropertyId {prop_id}. Error: {error_code}")
        
        return None

    def get_nvme_identify(self):
        """Read NVMe Identify Controller data using IOCTL_STORAGE_QUERY_PROPERTY"""
        if not self.handle:
            return None

        # Output buffer for STORAGE_PROTOCOL_DATA_DESCRIPTOR + 4096 bytes data (NVMe Identify is 4KB)
        output_buffer_size = ctypes.sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR) + 4096
        output_buffer = (ctypes.c_ubyte * output_buffer_size)()
        
        for prop_id in [28, 30]:
            query = STORAGE_PROPERTY_QUERY()
            query.PropertyId = prop_id
            query.QueryType = 0
            
            protocol_data = ctypes.cast(query.AdditionalParameters, ctypes.POINTER(STORAGE_PROTOCOL_SPECIFIC_DATA)).contents
            protocol_data.ProtocolType = ProtocolTypeNvme
            protocol_data.DataType = NVMeDataTypeIdentify
            protocol_data.ProtocolDataRequestValue = NVMeIdentifyController
            protocol_data.ProtocolDataRequestSubValue = 0
            protocol_data.ProtocolDataOffset = ctypes.sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR)
            protocol_data.ProtocolDataLength = 4096
            
            result, bytes_returned, error_code = Win32API.device_io_control(
                self.handle,
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(output_buffer),
                output_buffer_size
            )
            
            if result:
                descriptor = ctypes.cast(output_buffer, ctypes.POINTER(STORAGE_PROTOCOL_DATA_DESCRIPTOR)).contents
                offset = descriptor.ProtocolSpecificData.ProtocolDataOffset
                if offset > 0 and offset < output_buffer_size:
                    identify_data = bytes(output_buffer[offset : offset + 4096])
                    logging.info(f"Drive {self.drive_index}: NVMe IDENTIFY query successful with PropertyId {prop_id}")
                    return identify_data
        
        return None

    @staticmethod
    def parse_nvme_identify_data(data):
        """Parse NVMe Identify Controller data to extract model and serial"""
        if not data or len(data) < 1024:
            return None, None
            
        try:
            # Serial Number: bytes 4-23 (20 bytes)
            serial = data[4:24].decode('ascii', errors='ignore').strip()
            # Model Number: bytes 24-63 (40 bytes)
            model = data[24:64].decode('ascii', errors='ignore').strip()
            return model, serial
        except Exception:
            return None, None

    def send_scsi_command(self, cdb, data_buffer=None, data_direction=SCSI_IOCTL_DATA_IN, timeout=15):
        if not self.handle:
            return False, 0

        # Use a single buffer for SPTD + SenseInfo to ensure correct alignment and size
        class SPTD_WITH_SENSE(ctypes.Structure):
            _fields_ = [
                ("sptd", SCSI_PASS_THROUGH_DIRECT),
                ("sense", ctypes.c_ubyte * 32)
            ]

        sptdw = SPTD_WITH_SENSE()
        sptdw.sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
        sptdw.sptd.CdbLength = len(cdb)
        sptdw.sptd.DataIn = data_direction
        sptdw.sptd.TimeOutValue = timeout
        sptdw.sptd.SenseInfoLength = ctypes.sizeof(sptdw.sense)
        sptdw.sptd.SenseInfoOffset = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
        
        # Copy CDB
        for i, b in enumerate(cdb):
            sptdw.sptd.Cdb[i] = b

        if data_buffer is not None:
            sptdw.sptd.DataTransferLength = ctypes.sizeof(data_buffer)
            sptdw.sptd.DataBuffer = ctypes.cast(ctypes.pointer(data_buffer), ctypes.c_void_p)
        else:
            sptdw.sptd.DataTransferLength = 0
            sptdw.sptd.DataBuffer = None

        result, bytes_returned, error_code = Win32API.device_io_control(
            self.handle,
            IOCTL_SCSI_PASS_THROUGH_DIRECT,
            ctypes.byref(sptdw),
            ctypes.sizeof(sptdw),
            ctypes.byref(sptdw),
            ctypes.sizeof(sptdw)
        )
        
        if not result:
            # 25H2 specific: logging more details
            from ctypes import FormatError
            err_msg = FormatError(error_code).strip()
            cdb_hex = "".join([f"{b:02X}" for b in cdb])
            logging.error(f"Drive {self.drive_index}: DeviceIoControl (SCSI) failed. CDB: {cdb_hex}, Error: {error_code} ({hex(error_code)}) - {err_msg}")
            # If error is 1 (Incorrect function) or 50 (Not supported), it might be a driver/OS restriction
            return False, 0
        
        if sptdw.sptd.ScsiStatus != 0:
            sense = bytes(sptdw.sense)
            # Check if it's a SAT success with ATA registers (CHECK CONDITION + RECOVERED ERROR/NO SENSE + ASC=00, ASCQ=1D)
            if sptdw.sptd.ScsiStatus == 2: # CHECK CONDITION
                is_sat_success = False
                if sense[0] in (0x72, 0x73): # Descriptor format
                    sense_key = sense[1] & 0x0F
                    asc = sense[2]
                    ascq = sense[3]
                    if sense_key in (0x00, 0x01, 0x09) and asc == 0x00 and ascq == 0x1D:
                        is_sat_success = True
                elif sense[0] in (0x70, 0x71): # Fixed format
                    sense_key = sense[2] & 0x0F
                    asc = sense[12]
                    ascq = sense[13]
                    if sense_key in (0x00, 0x01, 0x09) and asc == 0x00 and ascq == 0x1D:
                        is_sat_success = True
                        
                if is_sat_success:
                    # The command actually succeeded, the device is just returning ATA registers
                    return True, bytes_returned

            sense_hex = sense.hex()
            cdb_hex = "".join([f"{b:02X}" for b in cdb])
            logging.error(f"Drive {self.drive_index}: SCSI Status {sptdw.sptd.ScsiStatus}, CDB: {cdb_hex}, Sense: {sense_hex}")
            return False, 0
            
        return True, bytes_returned
    
    def send_ata_pass_through(self, feature, sector_count, lba_low, lba_mid, lba_high, command, data_buffer=None):
        """Send ATA command using IOCTL_ATA_PASS_THROUGH (No SCSI Translation)"""
        if not self.handle:
            return False, 0
            
        apt_buff = ATA_PASS_THROUGH_EX_WITH_BUFFER()
        apt = apt_buff.apt
        apt.Length = ctypes.sizeof(ATA_PASS_THROUGH_EX)
        apt.AtaFlags = ATA_FLAGS_DRDY_REQUIRED | ATA_FLAGS_DATA_IN
        apt.TimeOutValue = 15
        
        # ATA Task File
        apt.PreviousTaskFile[0] = 0 # Feature / Error
        apt.PreviousTaskFile[1] = 0 # Sector Count
        apt.PreviousTaskFile[2] = 0 # LBA Low
        apt.PreviousTaskFile[3] = 0 # LBA Mid
        apt.PreviousTaskFile[4] = 0 # LBA High
        apt.PreviousTaskFile[5] = 0 # Device / Head
        apt.PreviousTaskFile[6] = 0 # Command / Status
        apt.PreviousTaskFile[7] = 0 # Reserved

        apt.CurrentTaskFile[0] = feature
        apt.CurrentTaskFile[1] = sector_count
        apt.CurrentTaskFile[2] = lba_low
        apt.CurrentTaskFile[3] = lba_mid
        apt.CurrentTaskFile[4] = lba_high
        apt.CurrentTaskFile[5] = 0xE0 # Device (LBA mode)
        apt.CurrentTaskFile[6] = command
        apt.CurrentTaskFile[7] = 0

        if data_buffer:
            apt.DataTransferLength = 512
            apt.DataBufferOffset = ctypes.sizeof(ATA_PASS_THROUGH_EX)
        else:
            apt.DataTransferLength = 0
            apt.DataBufferOffset = 0
            
        result, bytes_returned, error_code = Win32API.device_io_control(
            self.handle,
            IOCTL_ATA_PASS_THROUGH,
            ctypes.byref(apt_buff),
            ctypes.sizeof(apt_buff),
            ctypes.byref(apt_buff),
            ctypes.sizeof(apt_buff)
        )
        
        if not result:
            logging.warning(f"Drive {self.drive_index}: IOCTL_ATA_PASS_THROUGH failed. Error: {error_code} ({hex(error_code)})")
            return False, 0
            
        if data_buffer:
            ctypes.memmove(data_buffer, apt_buff.Data, 512)
            
        return True, bytes_returned

    def identify_device(self):
        """Read 512 bytes of IDENTIFY DEVICE data using SAT"""
        buffer = (ctypes.c_ubyte * 512)()
        
        # Attempt 1: ATA PASS-THROUGH (16)
        cdb = [0] * 16
        cdb[0] = 0x85 # ATA PASS-THROUGH (16)
        cdb[1] = (4 << 1) # PROTOCOL=4 (PIO Data-In)
        cdb[2] = 0x0A # CK_COND=0, T_DIR=1, BYTE_BLOCK=0, T_LENGTH=2
        cdb[6] = 0x01 # Sector Count
        cdb[14] = 0xEC # Command - IDENTIFY DEVICE

        success, _ = self.send_scsi_command(cdb, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)
            
        # Attempt 2: ATA PASS-THROUGH (12)
        cdb12 = [0] * 12
        cdb12[0] = 0xA1
        cdb12[1] = (4 << 1)
        cdb12[2] = 0x0A
        cdb12[4] = 0x01
        cdb12[9] = 0xEC
        
        success, _ = self.send_scsi_command(cdb12, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)
            
        # Attempt 3: IOCTL_ATA_PASS_THROUGH
        # Feature=0, SectorCount=1, LBA_Low=0, LBA_Mid=0, LBA_High=0, Command=0xEC
        success, _ = self.send_ata_pass_through(0, 0x01, 0, 0, 0, 0xEC, buffer)
        if success:
            return bytes(buffer)

        return None

    @staticmethod
    def parse_identify_data(data):
        """Parse ATA IDENTIFY DEVICE data to extract model and serial"""
        if not data or len(data) < 512:
            return None, None
            
        def decode_ata_string(offset, length):
            # ATA strings are swapped bytes (Big Endian in Word)
            try:
                raw_bytes = data[offset:offset+length]
                # Swap bytes: 01 02 03 04 -> 02 01 04 03
                swapped = bytearray(length)
                for i in range(0, length, 2):
                    swapped[i] = raw_bytes[i+1]
                    swapped[i+1] = raw_bytes[i]
                return swapped.decode('ascii', errors='ignore').strip()
            except Exception:
                return "Unknown"

        # Word 10-19: Serial Number (20 bytes) -> Offset 20
        serial = decode_ata_string(20, 20)
        
        # Word 27-46: Model Number (40 bytes) -> Offset 54
        model = decode_ata_string(54, 40)
        
        return model, serial

    def get_smart_data(self):
        """Read 512 bytes of SMART data using SAT (SCSI ATA Translation) or ATA Pass-Through"""
        buffer = (ctypes.c_ubyte * 512)()
        
        # --- Method 1: SAT-16 (SCSI ATA PASS-THROUGH 16) ---
        # SMART READ DATA: Command=B0, Feature=D0, LBA Mid=4F, LBA High=C2
        cdb16 = [0] * 16
        cdb16[0] = 0x85 # ATA PASS-THROUGH (16)
        cdb16[1] = (4 << 1) # PROTOCOL=4 (PIO Data-In)
        cdb16[2] = 0x2E # CK_COND=1, T_DIR=1, BYTE_BLOCK=1, T_LENGTH=2
        cdb16[4] = 0xD0 # Features (7:0)
        cdb16[6] = 0x01 # Sector Count (7:0)
        cdb16[8] = 0x00 # LBA Low (7:0)
        cdb16[10] = 0x4F # LBA Mid (15:8) - Cylinder Low
        cdb16[12] = 0xC2 # LBA High (23:16) - Cylinder High
        cdb16[14] = 0xB0 # Command - SMART

        success, _ = self.send_scsi_command(cdb16, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)
            
        logging.warning(f"Drive {self.drive_index}: SAT-16 SMART query failed, trying SAT-12...")

        # --- Method 2: SAT-12 (SCSI ATA PASS-THROUGH 12) ---
        cdb12 = [0] * 12
        cdb12[0] = 0xA1 # ATA PASS-THROUGH (12)
        cdb12[1] = (4 << 1) # PROTOCOL=4 (PIO Data-In)
        cdb12[2] = 0x2E # CK_COND=1, T_DIR=1, BYTE_BLOCK=1, T_LENGTH=2
        cdb12[3] = 0xD0 # Features
        cdb12[4] = 0x01 # Sector Count
        cdb12[5] = 0x00 # LBA Low
        cdb12[6] = 0x4F # LBA Mid
        cdb12[7] = 0xC2 # LBA High
        cdb12[9] = 0xB0 # Command - SMART
        
        success, _ = self.send_scsi_command(cdb12, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)

        logging.warning(f"Drive {self.drive_index}: SAT-12 SMART query failed, trying IOCTL_ATA_PASS_THROUGH...")

        # --- Method 3: IOCTL_ATA_PASS_THROUGH (Direct ATA) ---
        # Feature=0xD0, SectorCount=1, LBA_Low=0, LBA_Mid=0x4F, LBA_High=0xC2, Command=0xB0
        success, _ = self.send_ata_pass_through(0xD0, 0x01, 0, 0x4F, 0xC2, 0xB0, buffer)
        if success:
            return bytes(buffer)
            
        logging.error(f"Drive {self.drive_index}: All SMART query methods failed.")
        
        # --- Method 4: WMI Fallback (Final Attempt) ---
        try:
            # First, get model/serial to match WMI
            identify_data = self.identify_device()
            model, serial = self.parse_identify_data(identify_data)
            
            # Use hints if IDENTIFY failed
            if not serial:
                model = self.model_hint
                serial = self.serial_hint
                
            if not serial:
                return None
                
            import wmi
            c = wmi.WMI(namespace="root/wmi")
            # WMI SMART data
            for drive in c.MSStorageDriver_ATASmartData():
                # InstanceName contains Model and Serial often
                # Example: IDE\DiskWDC_WD10JPVX-22JC3T0_____________________1.04____\4&30030022&0&0.0.0_0
                instance_name = drive.InstanceName.upper()
                if serial.upper() in instance_name or (model and model.upper()[:10] in instance_name):
                    logging.info(f"Drive {self.drive_index}: Found matching WMI SMART data for {serial}")
                    return bytes(drive.VendorSpecific)
        except Exception as e:
            logging.warning(f"Drive {self.drive_index}: WMI SMART fallback failed: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            
        return None

    def spin_down(self):
        """Send ATA STANDBY IMMEDIATE to stop the motor"""
        if not self.handle:
            return False

        # Command=E0 (STANDBY IMMEDIATE)
        cdb16 = [0] * 16
        cdb16[0] = 0x85 # ATA PASS-THROUGH (16)
        cdb16[1] = (3 << 1) # PROTOCOL=3 (Non-data)
        cdb16[2] = 0x00 # CK_COND=0, T_DIR=0, T_LENGTH=0
        cdb16[14] = 0xE0 # Command - STANDBY IMMEDIATE
        
        logging.info(f"Drive {self.drive_index}: Sending ATA STANDBY IMMEDIATE...")
        success, _ = self.send_scsi_command(cdb16, None, data_direction=SCSI_IOCTL_DATA_OUT)
        if success:
            return True

        # Fallback: SCSI START STOP UNIT (Stop motor)
        cdb = [0] * 6
        cdb[0] = 0x1B # START STOP UNIT
        cdb[4] = 0x00 # Start=0 (Stop)
        logging.info(f"Drive {self.drive_index}: Sending SCSI START STOP UNIT (Stop)...")
        success, _ = self.send_scsi_command(cdb)
        return success

    def idle(self):
        """Send ATA IDLE IMMEDIATE (0xE1) to wake the drive from STANDBY/SLEEP."""
        if not self.handle:
            return False
        cdb = [0] * 16
        cdb[0] = 0x85
        cdb[1] = (3 << 1)
        cdb[14] = 0xE1
        logging.info(f"Drive {self.drive_index}: Sending ATA IDLE IMMEDIATE (wake)...")
        success, _ = self.send_scsi_command(cdb, None, data_direction=SCSI_IOCTL_DATA_OUT)
        return success

    def is_responding(self):
        """Lightweight SCSI TEST UNIT READY to check if drive is actually not sleeping."""
        if not self.handle:
            return False
        cdb = [0] * 6
        cdb[0] = 0x00  # TEST UNIT READY
        success, _ = self.send_scsi_command(cdb, None, data_direction=1, timeout=3)
        return success

    def sleep(self):
        """
        Send ATA SLEEP (0xE6) command. 
        Drive will enter lowest power state and won't wake up until reset/power cycle.
        Falls back to STANDBY IMMEDIATE if SLEEP fails.
        """
        if not self.handle:
            return False

        # 1. First, send ATA FLUSH CACHE (0xE7) to ensure all data is written to media
        # This is the key to preventing "Unexpected Power Loss" counts.
        flush_cdb = [0] * 16
        flush_cdb[0] = 0x85 # ATA PASS-THROUGH (16)
        flush_cdb[1] = (3 << 1) # PROTOCOL=3 (Non-data)
        flush_cdb[14] = 0xE7 # Command - FLUSH CACHE
        logging.info(f"Drive {self.drive_index}: Sending ATA FLUSH CACHE...")
        self.send_scsi_command(flush_cdb, None, data_direction=SCSI_IOCTL_DATA_OUT)

        # 2. Then send ATA SLEEP (0xE6)
        # This parks the heads and puts the drive in its deepest power-down state.
        sleep_cdb = [0] * 16
        sleep_cdb[0] = 0x85 # ATA PASS-THROUGH (16)
        sleep_cdb[1] = (3 << 1) # PROTOCOL=3 (Non-data)
        sleep_cdb[14] = 0xE6 # Command - SLEEP
        
        logging.info(f"Drive {self.drive_index}: Sending ATA SLEEP...")
        success, _ = self.send_scsi_command(sleep_cdb, None, data_direction=SCSI_IOCTL_DATA_OUT)
        if not success:
            logging.warning(f"Drive {self.drive_index}: ATA SLEEP failed, falling back to STANDBY IMMEDIATE...")
            return self.spin_down()
        return success

    def sleep_only(self):
        """只发送 ATA SLEEP (0xE6)，不 FLUSH CACHE。
        用于系统弹出场景：Windows 在 FS dismount 前已 flush 卷，无需重复 flush，
        且 SLEEP 的 IOCTL 快速返回，不会阻塞 DBT_DEVICEQUERYREMOVE 导致弹出被否决。"""
        if not self.handle:
            return False

        sleep_cdb = [0] * 16
        sleep_cdb[0] = 0x85  # ATA PASS-THROUGH (16)
        sleep_cdb[1] = (3 << 1)  # PROTOCOL=3 (Non-data)
        sleep_cdb[14] = 0xE6  # Command - SLEEP

        logging.info(f"Drive {self.drive_index}: Sending ATA SLEEP (only)...")
        # 弹出场景：只做一次快速尝试，失败立即返回，绝不回退到 STANDBY/START-STOP。
        # 原因：QUERYREMOVE 是同步窗口，多次 I/O 尝试会拖慢响应导致 Windows 弹出超时被否决
        # （系统事件日志 225）。且设备一旦开始移除，后续 I/O 会报 1117 (ERROR_IO_DEVICE)。
        success, _ = self.send_scsi_command(sleep_cdb, None, data_direction=SCSI_IOCTL_DATA_OUT, timeout=3)
        if not success:
            logging.warning(
                f"Drive {self.drive_index}: ATA SLEEP 失败（弹出场景不回退），"
                f"将立即返回以允许系统继续移除"
            )
            return False
        return success

    def set_standby_timer(self, minutes):
        """
        Set ATA Standby Timer using IDLE (0xE1) command via SAT.
        minutes: 0 to 60. 0 means disabled.
        """
        if not self.handle:
            return False

        # Map minutes to ATA Sector Count value
        if minutes == 0:
            count = 0 # Disabled
        elif 1 <= minutes <= 20:
            count = minutes * 12 # 5 seconds units (12 * 5s = 60s = 1min)
        elif minutes <= 30:
            count = 241 # 30 minutes
        elif minutes <= 60:
            count = 242 # 60 minutes
        else:
            count = 242 # Cap at 60 mins for this UI

        # ATA IDLE command (0xE1)
        cdb16 = [0] * 16
        cdb16[0] = 0x85 # ATA PASS-THROUGH (16)
        cdb16[1] = (3 << 1) # PROTOCOL=3 (Non-data)
        cdb16[2] = 0x00 # CK_COND=0, T_DIR=0, T_LENGTH=0
        cdb16[6] = count # Sector Count (7:0) - Timer value
        cdb16[14] = 0xE1 # Command - IDLE
        
        logging.info(f"Drive {self.drive_index}: Sending ATA IDLE (Set Standby Timer to {minutes} min, count={count})...")
        success, _ = self.send_scsi_command(cdb16, None, data_direction=SCSI_IOCTL_DATA_OUT)
        return success
