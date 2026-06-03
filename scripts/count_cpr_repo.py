import os

src_path = r"D:\DHRUV\Aii\nexus_prime\new chromeium profile!\chromium(Nexusprimeomega)"
dst_path = r"D:\DHRUV\Aii\nexus_prime\cpr_repo"

def get_stats(root_dir):
    num_files = 0
    num_dirs = 0
    total_size = 0
    for root, dirs, files in os.walk(root_dir):
        # Exclude .git directories in the target
        if ".git" in dirs:
            dirs.remove(".git")
        num_dirs += len(dirs)
        for f in files:
            num_files += 1
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
            except Exception:
                pass
    return num_files, num_dirs, total_size

if __name__ == "__main__":
    src_files, src_dirs, src_size = get_stats(src_path)
    dst_files, dst_dirs, dst_size = get_stats(dst_path)
    print(f"Source files: {src_files}, dirs: {src_dirs}, size: {src_size}")
    print(f"Dest files: {dst_files}, dirs: {dst_dirs}, size: {dst_size}")
    if src_files == dst_files and src_dirs == dst_dirs and src_size == dst_size:
        print("SUCCESS: Source and destination match perfectly!")
    else:
        print("WARNING: Mismatch detected!")
