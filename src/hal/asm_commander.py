import ctypes
import logging
from .win32_api import (
    Win32API, SCSI_PASS_THROUGH_DIRECT, IOCTL_SCSI_PASS_THROUGH_DIRECT, 
    SCSI_IOCTL_DATA_IN, SCSI_IOCTL_DATA_OUT, SCSI_PASS_THROUGH_DIRECT_WITH_SENSE,
    IOCTL_STORAGE_QUERY_PROPERTY, STORAGE_PROPERTY_QUERY, STORAGE_PROTOCOL_SPECIFIC_DATA,
    STORAGE_PROTOCOL_DATA_DESCRIPTOR, ProtocolTypeNvme, NVMeDataTypeLogPage, NVMeLogPageHealthInfo
)

class ASMCommander:
    def __init__(self, drive_index):
        self.drive_index = drive_index
        self.handle = None

    def __enter__(self):
        self.handle = Win32API.open_physical_drive(self.drive_index)
        if not self.handle:
            logging.error(f"DEBUG: Failed to open PhysicalDrive{self.drive_index}. Last error: {ctypes.get_last_error()}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.handle:
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

    def send_scsi_command(self, cdb, data_buffer=None, data_direction=SCSI_IOCTL_DATA_IN, timeout=5):
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
            logging.error(f"Drive {self.drive_index}: DeviceIoControl failed. Error: {error_code}")
            return False, 0
        
        if sptdw.sptd.ScsiStatus != 0:
            sense_hex = bytes(sptdw.sense).hex()
            logging.error(f"Drive {self.drive_index}: SCSI Status {sptdw.sptd.ScsiStatus}, Sense: {sense_hex}")
            return False, 0
            
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
        """Read 512 bytes of SMART data using SAT (SCSI ATA Translation)"""
        buffer = (ctypes.c_ubyte * 512)()
        
        # Try SAT-16 with different flags
        # Attempt 1: SAT-16 Standard
        # SMART READ DATA: Command=B0, Feature=D0, LBA Mid=4F, LBA High=C2
        cdb16 = [0] * 16
        cdb16[0] = 0x85 # ATA PASS-THROUGH (16)
        cdb16[1] = (4 << 1) # PROTOCOL=4 (PIO Data-In)
        cdb16[2] = 0x0A # CK_COND=0, T_DIR=1, BYTE_BLOCK=0, T_LENGTH=2
        cdb16[4] = 0xD0 # Features (7:0)
        cdb16[6] = 0x01 # Sector Count (7:0)
        # Byte 8: LBA Low (7:0) - 0
        cdb16[10] = 0x4F # LBA Mid (15:8) - Cylinder Low
        cdb16[12] = 0xC2 # LBA High (23:16) - Cylinder High
        cdb16[14] = 0xB0 # Command - SMART

        success, _ = self.send_scsi_command(cdb16, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)

        # Attempt 2: SAT-12
        cdb12 = [0] * 12
        cdb12[0] = 0xA1 # ATA PASS-THROUGH (12)
        cdb12[1] = (4 << 1) # PIO Data-In
        cdb12[2] = 0x0A
        cdb12[3] = 0xD0 # Features
        cdb12[4] = 0x01 # Sector Count
        # Byte 5: LBA Low - 0
        cdb12[6] = 0x4F # LBA Mid
        cdb12[7] = 0xC2 # LBA High
        cdb12[9] = 0xB0 # Command

        success, _ = self.send_scsi_command(cdb12, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)

        # Attempt 3: SAT-16 with CK_COND=1 (Some ASMedia bridges like this)
        cdb16[2] = 0x2A # CK_COND=1, T_DIR=1, BYTE_BLOCK=0, T_LENGTH=2
        success, _ = self.send_scsi_command(cdb16, buffer, SCSI_IOCTL_DATA_IN)
        if success:
            return bytes(buffer)

        logging.error(f"Drive {self.drive_index}: All SAT attempts failed.")
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

    def sleep(self):
        """
        Send ATA SLEEP (0xE6) command. 
        Drive will enter lowest power state and won't wake up until reset/power cycle.
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
