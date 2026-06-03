import os

src_path = r"D:\DHRUV\Aii\nexus_prime\new chromeium profile!\chromium(Nexusprimeomega)"

def get_stats(root_dir):
    num_files = 0
    num_dirs = 0
    total_size = 0
    for root, dirs, files in os.walk(root_dir):
        num_dirs += len(dirs)
        for f in files:
            num_files += 1
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
            except Exception as e:
                pass
    return num_files, num_dirs, total_size

if __name__ == "__main__":
    if not os.path.exists(src_path):
        print(f"Error: Path {src_path} does not exist!")
    else:
        num_files, num_dirs, total_size = get_stats(src_path)
        print(f"Source Directory: {src_path}")
        print(f"Files: {num_files}")
        print(f"Folders: {num_dirs}")
        print(f"Total Size: {total_size} bytes ({total_size / (1024*1024):.2f} MB)")
