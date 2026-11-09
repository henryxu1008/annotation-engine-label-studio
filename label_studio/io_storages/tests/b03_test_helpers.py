import os
import subprocess


def create_directory_alias(link, target):
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_directory_alias(link):
    if os.name == 'nt':
        os.rmdir(link)
    else:
        link.unlink()
