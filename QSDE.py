# Qustellar Smart Download Engine (QSDE)
# Version: 1.0.0 (Stable / PascalCase Edition)
# Copyright (c) 2026 Qustellar. All rights reserved.
# Licensed under the Apache License, Version 2.0

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Callable, Dict, Union, Any
from dataclasses import dataclass
from enum import Enum

# Third-party libraries
import httpx
import aiofiles

# Rich TUI libraries
from rich.progress import (
    Progress,
    ProgressColumn,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TaskID,
    Task
)
from rich.console import Console
from rich.panel import Panel
from rich.theme import Theme
from rich.text import Text
from rich.filesize import decimal

# --- Global Configuration ---
# Suppress INFO logs from underlying libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Setup standard logger for Headless mode
logging.basicConfig(format='[%(levelname)s] %(message)s', level=logging.INFO)
Logger = logging.getLogger("QSDE")

# Rich Theme
CustomTheme = Theme({
    "info": "dim cyan",
    "warning": "magenta",
    "error": "bold red",
    "success": "bold green",
    "bar.back": "grey23",
    "bar.complete": "deep_sky_blue1",
    "bar.finished": "green",
    "bar.pulse": "bright_magenta"
})

GlobalConsole = Console(theme=CustomTheme)

# --- Enums & Helpers ---

class HashAlgorithm(Enum):
    """Supported hash algorithms."""
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SHA512 = "sha512"

class SmartUnitColumn(ProgressColumn):
    """Rich Column: 'Files' for batch, 'Bytes' for single."""
    def render(self, task: Task) -> Text:
        if task.fields.get("type") == "overall":
            Completed = int(task.completed)
            Total = int(task.total)
            return Text(f"{Completed}/{Total} Files", style="bright_white")
        
        Completed = int(task.completed)
        Total = int(task.total) if task.total else None
        if Total is None:
            return Text(f"{decimal(Completed)}", style="green")
        return Text(f"{decimal(Completed)}/{decimal(Total)}", style="green")

class SmartSpeedColumn(ProgressColumn):
    """Rich Column: Speed for downloads only."""
    def render(self, task: Task) -> Text:
        if task.fields.get("type") == "overall":
            return Text("") 
        Speed = task.speed
        if Speed is None:
            return Text("?", style="dim")
        return Text(f"{decimal(Speed)}/s", style="red")

# --- Core Data Structures ---

@dataclass
class DownloadTask:
    """
    Represents a single file download task.
    """
    Url: str
    SavePath: str
    ExpectedHash: Optional[str] = None
    HashAlgo: HashAlgorithm = HashAlgorithm.SHA256

# --- Main Engine ---

class QSDEngine:
    """
    Qustellar Smart Download Engine (QSDE) v1.0.0
    """

    def __init__(
        self,
        MaxConcurrency: int = 16,
        TimeOut: int = 30,
        MaxRetries: int = 3,
        Proxy: Optional[str] = None,
        UserAgent: str = "QSDE/1.0.0 (High Performance)",
        VerifySSL: bool = True,
        ChunkSize: int = 65536,
        EnableUI: bool = True
    ):
        """
        Initialize the engine.
        """
        self.MaxConcurrency = MaxConcurrency
        self.TimeOut = TimeOut
        self.MaxRetries = MaxRetries
        self.Proxy = Proxy
        self.UserAgent = UserAgent
        self.VerifySSL = VerifySSL
        self.ChunkSize = ChunkSize
        self.EnableUI = EnableUI
        self.Console = GlobalConsole
        
        self._ProgressCallback: Optional[Callable[[int, int], None]] = None
        self._ByteProgressCallback: Optional[Callable[[str, int, int], None]] = None
        self._CancelEvent = asyncio.Event()

    # --- Customization Methods (PascalCase) ---

    def SetCallbacks(
        self, 
        OnTotalProgress: Optional[Callable[[int, int], None]] = None,
        OnByteProgress: Optional[Callable[[str, int, int], None]] = None
    ) -> None:
        """Set external callbacks for progress tracking."""
        self._ProgressCallback = OnTotalProgress
        self._ByteProgressCallback = OnByteProgress

    def SetNetworkConfig(
        self, 
        Proxy: Optional[str] = None, 
        UserAgent: Optional[str] = None,
        TimeOut: Optional[int] = None
    ) -> None:
        """Dynamically update network configurations."""
        if Proxy is not None: self.Proxy = Proxy
        if UserAgent is not None: self.UserAgent = UserAgent
        if TimeOut is not None: self.TimeOut = TimeOut
        self._LogMessage("info", f"Network config updated. UA: {self.UserAgent}")

    def SetRuntimeConfig(
        self,
        MaxConcurrency: Optional[int] = None,
        ChunkSize: Optional[int] = None
    ) -> None:
        """Dynamically update runtime performance settings."""
        if MaxConcurrency is not None: self.MaxConcurrency = MaxConcurrency
        if ChunkSize is not None: self.ChunkSize = ChunkSize

    def CancelAll(self) -> None:
        """Signals all running tasks to cancel immediately."""
        self._CancelEvent.set()
        if self.EnableUI:
            self.Console.print("[bold red]Cancellation signal received![/]")
        else:
            Logger.warning("Cancellation signal received.")

    # --- Internal Logic (PascalCase Wrapper) ---

    @staticmethod
    def _SanitizeFilename(PathStr: str) -> str:
        """Sanitizes filename."""
        PathObj = Path(PathStr)
        CleanName = re.sub(r'[<>:"/\\|?*]', '_', PathObj.name)
        return str(PathObj.with_name(CleanName))

    def _LogMessage(self, Level: str, Message: str, ProgressManager: Optional[Progress] = None):
        """Unified logging: Prints to Rich Console if UI is on, else standard Logger."""
        if self.EnableUI and ProgressManager:
            # Print to console so it persists after progress bar clears
            Style = "white"
            if Level == "error": Style = "bold red"
            elif Level == "warning": Style = "yellow"
            elif Level == "info": Style = "dim cyan"
            
            ProgressManager.console.print(f"[{Style}]{Message}[/]")
        else:
            if Level == "error": Logger.error(Message)
            elif Level == "warning": Logger.warning(Message)
            else: Logger.info(Message)

    async def _CalculateHash(self, FilePath: str, Algo: HashAlgorithm, ProgressManager: Optional[Progress], TaskId: Optional[TaskID]) -> str:
        """Calculates hash with cancellation support."""
        if not os.path.exists(FilePath):
            return ""
        
        if self.EnableUI and ProgressManager:
            ProgressManager.update(TaskId, status="[yellow]Verifying[/]")
        
        HashObj = hashlib.new(Algo.value)
        
        async with aiofiles.open(FilePath, "rb") as f:
            while True:
                if self._CancelEvent.is_set():
                    raise asyncio.CancelledError
                Chunk = await f.read(self.ChunkSize)
                if not Chunk:
                    break
                HashObj.update(Chunk)
        
        return HashObj.hexdigest()

    async def _DownloadSingleFile(
        self, 
        Client: httpx.AsyncClient, 
        TaskItem: DownloadTask, 
        Semaphore: asyncio.Semaphore,
        ProgressManager: Optional[Progress],
        RichTaskId: Optional[TaskID]
    ) -> bool:
        """
        Handles single file download logic.
        """
        SafeSavePath = self._SanitizeFilename(TaskItem.SavePath)
        DisplayName = Path(SafeSavePath).name
        TempPath = f"{SafeSavePath}.tmp"
        
        async with Semaphore:
            try:
                for Attempt in range(self.MaxRetries):
                    if self._CancelEvent.is_set():
                        raise asyncio.CancelledError

                    try:
                        # UI Update: Connecting
                        if self.EnableUI and ProgressManager:
                            ProgressManager.update(RichTaskId, status="[cyan]Connecting[/]", visible=True)
                        
                        # Headers & Resume Logic
                        ExistingSize = 0
                        Headers = {"User-Agent": self.UserAgent}
                        if os.path.exists(TempPath):
                            ExistingSize = os.path.getsize(TempPath)
                            Headers["Range"] = f"bytes={ExistingSize}-"
                        
                        Request = Client.build_request("GET", TaskItem.Url, headers=Headers)
                        Response = await Client.send(Request, stream=True)
                        
                        # Handle 416 (Reset)
                        if Response.status_code == 416:
                            if os.path.exists(TempPath): os.remove(TempPath)
                            ExistingSize = 0
                            Headers.pop("Range", None)
                            Request = Client.build_request("GET", TaskItem.Url, headers=Headers)
                            Response = await Client.send(Request, stream=True)

                        if Response.status_code not in (200, 206):
                            Response.raise_for_status()

                        RemoteSize = int(Response.headers.get("Content-Length", 0))
                        TotalSize = RemoteSize + ExistingSize if RemoteSize > 0 else None
                        Mode = "ab" if Response.status_code == 206 else "wb"

                        # UI Update: Downloading
                        if self.EnableUI and ProgressManager:
                            ProgressManager.update(
                                RichTaskId, 
                                total=TotalSize, 
                                completed=ExistingSize, 
                                status="[blue]Downloading[/]"
                            )
                        
                        # Stream Write
                        async with aiofiles.open(TempPath, Mode) as f:
                            async for Chunk in Response.aiter_bytes(chunk_size=self.ChunkSize):
                                if self._CancelEvent.is_set():
                                    raise asyncio.CancelledError
                                
                                if Chunk:
                                    await f.write(Chunk)
                                    ChunkLen = len(Chunk)
                                    
                                    # Callbacks
                                    if self.EnableUI and ProgressManager:
                                        ProgressManager.update(RichTaskId, advance=ChunkLen)
                                    if self._ByteProgressCallback:
                                        self._ByteProgressCallback(DisplayName, ChunkLen, TotalSize or 0)

                        # Hash Verification
                        if TaskItem.ExpectedHash:
                            FileHash = await self._CalculateHash(TempPath, TaskItem.HashAlgo, ProgressManager, RichTaskId)
                            if FileHash.lower() != TaskItem.ExpectedHash.lower():
                                self._LogMessage("warning", f"⚠ Hash mismatch for {DisplayName}. Retrying...", ProgressManager)
                                if os.path.exists(TempPath): os.remove(TempPath)
                                continue 
                        
                        # Success Rename
                        if os.path.exists(SafeSavePath):
                            os.remove(SafeSavePath)
                        os.rename(TempPath, SafeSavePath)
                        
                        if self.EnableUI and ProgressManager:
                            ProgressManager.update(RichTaskId, status="[bold green]Done[/]", visible=False)
                        return True

                    except (httpx.RequestError, httpx.HTTPStatusError, OSError, asyncio.TimeoutError) as e:
                        # Log Retry
                        IsFatal = isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 404)
                        if Attempt < self.MaxRetries - 1 and not IsFatal:
                            WaitTime = (Attempt + 1) * 2
                            self._LogMessage("info", f"↺ Retry {Attempt+1}/{self.MaxRetries} for {DisplayName} in {WaitTime}s...", ProgressManager)
                            
                            if self.EnableUI and ProgressManager:
                                ProgressManager.update(RichTaskId, status=f"[magenta]Waiting {WaitTime}s[/]")
                            
                            await asyncio.sleep(WaitTime)
                        else:
                            # Final Failure: Log explicitly so it persists
                            self._LogMessage("error", f"✖ Failed to download {DisplayName}: {e}", ProgressManager)
                            return False
                    
                return False

            except asyncio.CancelledError:
                if self.EnableUI and ProgressManager:
                    ProgressManager.update(RichTaskId, status="[red]Cancelled[/]")
                return False
            
            finally:
                # Cleanup .tmp if failed
                if not os.path.exists(SafeSavePath) and os.path.exists(TempPath):
                    try:
                        os.remove(TempPath)
                    except OSError:
                        pass
                # Hide bar
                if self.EnableUI and ProgressManager:
                    ProgressManager.update(RichTaskId, visible=False)

    async def _RunWrapper(self, Client, TaskItem, Semaphore, ProgressManager, FileTaskId, OverallTask):
        """Wrapper to update overall progress."""
        if self.EnableUI and ProgressManager:
            ProgressManager.start_task(FileTaskId)
        
        Result = await self._DownloadSingleFile(Client, TaskItem, Semaphore, ProgressManager, FileTaskId)
        
        if self.EnableUI and ProgressManager and OverallTask is not None:
            ProgressManager.update(OverallTask, advance=1)
        
        if self._ProgressCallback:
            # Trigger generic callback
            pass 
            
        return Result

    async def StartBatchDownload(self, Tasks: List[DownloadTask]) -> Dict[str, int]:
        """
        Start the batch download process.
        """
        self._CancelEvent.clear()
        Semaphore = asyncio.Semaphore(self.MaxConcurrency)
        
        TimeoutConfig = httpx.Timeout(connect=10.0, read=self.TimeOut, write=self.TimeOut, pool=10.0)
        Limits = httpx.Limits(max_keepalive_connections=self.MaxConcurrency, max_connections=self.MaxConcurrency + 10)
        
        # Prepare folders
        for TaskItem in Tasks:
            try:
                Path(self._SanitizeFilename(TaskItem.SavePath)).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

        if self.EnableUI:
            self.Console.print(f"[bold cyan]QSDE v1.0.0[/] | Processing [bold]{len(Tasks)}[/] files...")
        else:
            Logger.info(f"QSDE v1.0.0 started. Batch size: {len(Tasks)}")

        # --- Execution Logic (UI vs Headless) ---
        
        async def RunTasks(PMgr=None, OTask=None):
            async with httpx.AsyncClient(
                timeout=TimeoutConfig, 
                verify=self.VerifySSL,
                http2=True,
                proxy=self.Proxy, 
                limits=Limits,
                follow_redirects=True
            ) as Client:
                Coros = []
                for TaskItem in Tasks:
                    # Setup UI task ID if enabled
                    FileTaskId = None
                    if self.EnableUI and PMgr:
                        SafeName = Path(self._SanitizeFilename(TaskItem.SavePath)).name
                        FileTaskId = PMgr.add_task(
                            "waiting", filename=SafeName, visible=False, start=False,
                            status="[dim]Pending[/]", total=None, type="file"
                        )
                    
                    Coros.append(self._RunWrapper(
                        Client, TaskItem, Semaphore, PMgr, FileTaskId, OTask
                    ))
                
                return await asyncio.gather(*Coros, return_exceptions=True)

        # Run based on mode
        Results = []
        if self.EnableUI:
            # Rich Context Manager
            Columns = [
                SpinnerColumn("dots2"),
                TextColumn("[bold blue]{task.fields[filename]}", justify="left"),
                BarColumn(bar_width=40),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                "•", SmartUnitColumn(), 
                "•", SmartSpeedColumn(),
                "•", TimeRemainingColumn(), 
                "•", TextColumn("{task.fields[status]}")
            ]
            
            with Progress(*Columns, console=self.Console, transient=True, expand=True, refresh_per_second=30) as PMgr:
                OTask = PMgr.add_task("Batch Progress", total=len(Tasks), filename="[bold white]Overall[/]", status="[white]Running[/]", type="overall")
                try:
                    Results = await RunTasks(PMgr, OTask)
                except asyncio.CancelledError:
                    Results = [False] * len(Tasks)
        else:
            # Headless execution
            try:
                Results = await RunTasks(None, None)
            except asyncio.CancelledError:
                Results = [False] * len(Tasks)

        # Summary Stats
        SuccessCount = sum(1 for r in Results if r is True)
        FailCount = len(Results) - SuccessCount
        
        if self.EnableUI:
            Color = "green" if FailCount == 0 else "red"
            self.Console.print(Panel(
                f"Total Files: {len(Tasks)}\n[green]Success: {SuccessCount}[/]\n[red]Failed:  {FailCount}[/]",
                title="[bold]QSDE Summary[/]", border_style=Color, expand=False
            ))
        else:
            Logger.info(f"Batch completed. Success: {SuccessCount}, Failed: {FailCount}")

        return {"success": SuccessCount, "failed": FailCount}

# ---------------------------------------------------------
# Usage Demo (Example)
# ---------------------------------------------------------
if __name__ == "__main__":
    async def Demo():
        # Setup: Create dummy 'downloads' folder
        Path("downloads").mkdir(exist_ok=True)

        print("--- QSDE 1.0.0 Demo ---")
        Mode = input("Select Mode (1=GUI/Rich, 2=API/Silent): ").strip()
        UseUI = Mode == "1"

        # 1. Initialize Engine (PascalCase style arguments not required for __init__, but keeping code clean)
        Engine = QSDEngine(
            MaxConcurrency=4,
            VerifySSL=True,
            EnableUI=UseUI
        )

        # 2. Customize Configuration (Example of new feature)
        Engine.SetNetworkConfig(UserAgent="OLAN-Launcher/2.0 CustomUserAgent")
        # Engine.SetNetworkConfig(Proxy="http://127.0.0.1:7890") # Uncomment to test proxy

        # 3. Define Tasks
        Tasks = [
            DownloadTask(
                Url="http://speedtest.tele2.net/10MB.zip", 
                SavePath="downloads/test_10mb.dat"
            ),
            DownloadTask(
                Url="https://proof.ovh.net/files/1Mb.dat", 
                SavePath="downloads/test_1mb.dat"
            ),
            # Fail Test
            DownloadTask(
                Url="https://invalid-url-for-test.com/missing.exe", 
                SavePath="downloads/fail_test.exe"
            )
        ]
        
        # 4. Start
        if not UseUI:
            print("Running in background (check logs)...")
        
        await Engine.StartBatchDownload(Tasks)

    try:
        if os.name == 'nt':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(Demo())
    except KeyboardInterrupt:
        pass