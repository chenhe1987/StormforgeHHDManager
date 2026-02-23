import zipfile
import os
import time

def zip_directory(directory_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(directory_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, os.path.dirname(directory_path))
                try:
                    zipf.write(file_path, arcname)
                except Exception as e:
                    print(f"Error zipping {file_path}: {e}")
                    # If file is locked, wait and retry once
                    try:
                        time.sleep(1)
                        zipf.write(file_path, arcname)
                    except Exception as e2:
                        print(f"Retry failed for {file_path}: {e2}")

if __name__ == "__main__":
    src_dir = r"dist\疾风知硬盘柜管理_v1.3.33"
    dst_zip = r"dist\疾风知硬盘柜管理_v1.3.33.zip"
    if os.path.exists(src_dir):
        print(f"Zipping {src_dir} to {dst_zip}...")
        zip_directory(src_dir, dst_zip)
        print("Done.")
    else:
        print(f"Directory {src_dir} not found.")
