import sys
import os
import logging
from src.hal.asm_commander import ASMCommander
from src.utils.smart_parser import SmartParser
from src.utils.admin import run_as_admin

# Configure logging to file
logging.basicConfig(
    level=logging.DEBUG, 
    format='%(asctime)s %(levelname)s: %(message)s',
    filename='debug_disk_4.log',
    filemode='w',
    encoding='utf-8'
)
# Also print to console
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
logging.getLogger('').addHandler(console)

def test_specific_disk(index):
    logging.info(f"--- 正在测试磁盘 {index} ---")
    print(f"--- 正在测试磁盘 {index} ---")
    try:
        with ASMCommander(index) as cmd:
            logging.info("尝试 IDENTIFY DEVICE...")
            print("尝试 IDENTIFY DEVICE...")
            try:
                id_data = cmd.identify_device()
                if id_data:
                    logging.info("IDENTIFY DEVICE 成功!")
                    print("IDENTIFY DEVICE 成功!")
                    # 序列号在 10-19 字 (20-39 字节)
                    sn = id_data[20:40].decode('ascii', 'ignore').strip()
                    logging.info(f"磁盘序列号: {sn}")
                    print(f"磁盘序列号: {sn}")
                else:
                    logging.info("IDENTIFY DEVICE 失败。")
                    print("IDENTIFY DEVICE 失败。")
            except Exception as e:
                logging.error(f"IDENTIFY DEVICE 异常: {e}")
                print(f"IDENTIFY DEVICE 异常: {e}")

            logging.info("\n尝试读取 SMART 数据...")
            print("\n尝试读取 SMART 数据...")
            try:
                data = cmd.get_smart_data()
                if data:
                    logging.info(f"成功读取磁盘 {index} 的 SMART 数据!")
                    print(f"成功读取磁盘 {index} 的 SMART 数据!")
                    try:
                        attributes = SmartParser.parse_512(data)
                        for attr in attributes:
                            logging.info(str(attr))
                            print(attr)
                    except Exception as e:
                        logging.error(f"SMART 解析异常: {e}")
                else:
                    logging.info(f"无法读取磁盘 {index} 的 SMART 数据。请检查 debug.log 获取详细错误。")
                    print(f"无法读取磁盘 {index} 的 SMART 数据。请检查 debug.log 获取详细错误。")
            except Exception as e:
                logging.error(f"读取 SMART 数据异常: {e}")
                print(f"读取 SMART 数据异常: {e}")
    except Exception as e:
        logging.error(f"无法打开磁盘 {index}: {e}")

if __name__ == "__main__":
    if run_as_admin():
        disk_index = 4 # 默认测试磁盘 4
        if len(sys.argv) > 1:
            try:
                disk_index = int(sys.argv[1])
            except ValueError:
                pass
        test_specific_disk(disk_index)
        input("按回车键退出...")
