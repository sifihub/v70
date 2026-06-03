import os

src_path = r"D:\DHRUV\Aii\zara\new chromium\chromium101"
dst_path = r"D:\DHRUV\Aii\zara\cpr_repo"

def get_stats(root_dir):
    num_files = 0
    num_dirs = 0
    total_size = 0
    for root, dirs, files in os.walk(root_dir):
        # Exclude .git directory
        if ".git" in dirs:
            dirs.remove(".git")
        num_dirs += len(dirs)
        num_files += len(files)
        for f in files:
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
            except Exception:
                pass
    return num_files, num_dirs, total_size

src_files, src_dirs, src_size = get_stats(src_path)
dst_files, dst_dirs, dst_size = get_stats(dst_path)

print(f"Source path: {src_path}")
print(f"  Files  : {src_files}")
print(f"  Folders: {src_dirs}")
print(f"  Size   : {src_size} bytes")

print(f"\nDestination path: {dst_path}")
print(f"  Files  : {dst_files}")
print(f"  Folders: {dst_dirs}")
print(f"  Size   : {dst_size} bytes")

print(f"\nDifference:")
print(f"  File Diff  : {dst_files - src_files}")
print(f"  Folder Diff: {dst_dirs - src_dirs}")
print(f"  Size Diff  : {dst_size - src_size} bytes")
