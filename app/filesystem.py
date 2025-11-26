"""
FileSystem abstraction layer to support both Local and Network (SMB) storage.
Allows the application to work transparently with files regardless of their location.
"""

import os
import shutil
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Union, Any, Optional
from pathlib import Path

# Try to import smbclient, but don't fail if not installed (graceful degradation)
try:
    import smbclient
    from smbprotocol.exceptions import SMBResponseException
    SMB_AVAILABLE = True
except ImportError:
    SMB_AVAILABLE = False
    smbclient = None

logger = logging.getLogger(__name__)


class FileSystemProvider(ABC):
    """Abstract base class for file system operations."""
    
    @abstractmethod
    def list_directories(self, path: str) -> List[Dict[str, Any]]:
        """List directories in the given path."""
        pass
    
    @abstractmethod
    def create_directory(self, path: str) -> None:
        """Create a directory."""
        pass
        
    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if path exists."""
        pass
        
    @abstractmethod
    def is_dir(self, path: str) -> bool:
        """Check if path is a directory."""
        pass
        
    @abstractmethod
    def list_files(self, path: str, pattern: str = "*") -> List[str]:
        """List files in directory matching pattern."""
        pass
        
    @abstractmethod
    def move_file(self, src: str, dst: str) -> None:
        """Move a file from src to dst."""
        pass
        
    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read file content."""
        pass
        
    @abstractmethod
    def get_full_path(self, path: str) -> str:
        """Get full/absolute path."""
        pass
        
    @abstractmethod
    def join_path(self, *args) -> str:
        """Join path components."""
        pass


class LocalFileSystem(FileSystemProvider):
    """Implementation for local file system."""
    
    def list_directories(self, path: str) -> List[Dict[str, Any]]:
        path = path.strip() or "/"
        if not os.path.exists(path):
            return []
            
        result = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir():
                        result.append({
                            "name": entry.name,
                            "path": entry.path,
                            "has_children": True,  # Simplified assumption
                            "access_denied": False
                        })
            result.sort(key=lambda x: x["name"].lower())
        except PermissionError:
            logger.warning(f"Permission denied accessing {path}")
            # If we can't read the dir, we return empty list or indicate error
            # For the browser UI, we might want to show the dir exists but is locked
            pass
        except Exception as e:
            logger.error(f"Error listing directories in {path}: {e}")
            
        return result
    
    def create_directory(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        
    def exists(self, path: str) -> bool:
        return os.path.exists(path)
        
    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)
        
    def list_files(self, path: str, pattern: str = "*") -> List[str]:
        if not self.exists(path):
            return []
        
        # Simple glob matching
        import glob
        full_pattern = os.path.join(path, pattern)
        return glob.glob(full_pattern)
        
    def move_file(self, src: str, dst: str) -> None:
        # Ensure destination directory exists
        dst_dir = os.path.dirname(dst)
        if not self.exists(dst_dir):
            self.create_directory(dst_dir)
        shutil.move(src, dst)
        
    def read_file(self, path: str) -> bytes:
        with open(path, 'rb') as f:
            return f.read()
            
    def get_full_path(self, path: str) -> str:
        return os.path.abspath(path)
        
    def join_path(self, *args) -> str:
        return os.path.join(*args)


class SMBFileSystem(FileSystemProvider):
    """Implementation for SMB/CIFS network storage."""
    
    def __init__(self, host, share, username, password, domain=""):
        if not SMB_AVAILABLE:
            raise RuntimeError("smbclient library not installed")

        if not host or not share:
            raise RuntimeError("SMB Host und Freigabe müssen gesetzt sein.")
        if not username or not password:
            raise RuntimeError("SMB Benutzername und Passwort dürfen nicht leer sein.")
            
        self.host = host
        self.share = share
        self.username = username
        self.password = password
        self.domain = domain
        
        # Register session
        # Note: smbclient handles session reuse internally
        try:
            smbclient.register_session(
                host, 
                username=username, 
                password=password,
                domain=domain if domain else None
            )
        except Exception as e:
            logger.error(f"Failed to register SMB session: {e}")
            raise

    def _get_smb_path(self, path: str) -> str:
        """Convert path to UNC format: \\server\share\path"""
        # Clean up path
        path = path.replace("/", "\\")
        if path.startswith("\\"):
            path = path.lstrip("\\")
            
        # If path already includes server/share, try to respect it
        # But usually we work relative to the share or the provided host
        if path.startswith(f"{self.host}"):
            return f"\\\\{path}"
            
        return f"\\\\{self.host}\\{self.share}\\{path}"

    def list_directories(self, path: str) -> List[Dict[str, Any]]:
        full_path = self._get_smb_path(path)
        result = []
        try:
            for filename in smbclient.listdir(full_path):
                file_path = smbclient.path.join(full_path, filename)
                if smbclient.path.isdir(file_path):
                    result.append({
                        "name": filename,
                        "path": filename, # Keep relative to current browse path if possible? 
                                        # Or just use the name for navigation in frontend
                        "has_children": True,
                        "access_denied": False
                    })
            result.sort(key=lambda x: x["name"].lower())
        except Exception as e:
            logger.error(f"SMB list error: {e}")
            # raise e # Or handle gracefully
            
        return result

    def create_directory(self, path: str) -> None:
        smbclient.makedirs(self._get_smb_path(path), exist_ok=True)
        
    def exists(self, path: str) -> bool:
        try:
            return smbclient.path.exists(self._get_smb_path(path))
        except:
            return False
            
    def is_dir(self, path: str) -> bool:
        try:
            return smbclient.path.isdir(self._get_smb_path(path))
        except:
            return False
            
    def list_files(self, path: str, pattern: str = "*") -> List[str]:
        # smbclient doesn't have glob, so we filter listdir
        full_path = self._get_smb_path(path)
        files = []
        try:
            import fnmatch
            for filename in smbclient.listdir(full_path):
                if fnmatch.fnmatch(filename, pattern):
                    files.append(os.path.join(path, filename)) # Return 'relative' path for internal use
        except Exception as e:
            logger.error(f"SMB list files error: {e}")
            
        return files
        
    def move_file(self, src: str, dst: str) -> None:
        # Note: Moving between filesystems (Local <-> SMB) needs special handling
        # This generic move assumes source and dest are on THIS filesystem
        # If the app mixes them, we need higher level logic.
        # But for now, assuming Source and Target are both on SMB if mode is SMB.
        
        src_full = self._get_smb_path(src)
        dst_full = self._get_smb_path(dst)
        
        # Ensure dest dir exists
        dst_dir = smbclient.path.dirname(dst_full)
        if not smbclient.path.exists(dst_dir):
            smbclient.makedirs(dst_dir)
            
        smbclient.rename(src_full, dst_full)
        
    def read_file(self, path: str) -> bytes:
        with smbclient.open_file(self._get_smb_path(path), mode='rb') as f:
            return f.read()
            
    def get_full_path(self, path: str) -> str:
        return self._get_smb_path(path)
        
    def join_path(self, *args) -> str:
        # Use simple string join with forward slashes for internal logic
        # The SMB path converter will handle backslashes
        return "/".join(args).replace("//", "/")


def get_filesystem(settings: dict) -> FileSystemProvider:
    """Factory to get the configured file system provider."""
    storage_type = settings.get("storage_type", "local")
    
    if storage_type == "smb":
        return SMBFileSystem(
            host=settings.get("smb_host", ""),
            share=settings.get("smb_share", ""),
            username=settings.get("smb_username", ""),
            password=settings.get("smb_password", ""),
            domain=settings.get("smb_domain", "")
        )
    else:
        return LocalFileSystem()
