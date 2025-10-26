import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
from collections import deque
import uuid
import os
from queue import Queue, Empty
import copy

# --- Attempt to import PyPDF2 for PDF page counting ---
try:
    import PyPDF2
    PYPDF2_AVAILABLE = True
except ImportError:
    PYPDF2_AVAILABLE = False

# --- Data Structure for a Print Job ---
class PrintJob:
    """Represents a single print job with its properties."""
    def __init__(self, file_name, file_path, pages):
        self.job_id = str(uuid.uuid4())[:8]
        self.file_name = file_name
        self.file_path = file_path
        self.pages = pages # Total pages
        self.status = "Queued" # Queued, Printing, Paused, Completed, Canceled, Preempted
        self.progress = 0
        self.pages_remaining = pages

    def __repr__(self):
        return f"ID: {self.job_id} | File: {self.file_name} ({self.pages}p, {self.pages_remaining} left) | Status: {self.status}"

    def get_snapshot(self):
        """Returns a copy of the job's current state for thread-safe UI updates."""
        return copy.copy(self)

# --- Printer Worker Thread ---
class Printer(threading.Thread):
    """A worker thread representing a single printer."""
    
    def __init__(self, printer_id, printer_name, manager, update_queue):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.printer_name = printer_name # For display
        self.manager = manager
        self.update_queue = update_queue 
        
        self.current_job = None
        self.is_running = True
        self.is_paused = threading.Event()
        self.shutdown_flag = threading.Event() 
        
        self.interrupt_lock = threading.Lock()
        self.preempt_with_job = None
        self.cancel_current_job = False

    def log(self, message):
        """Sends a log message to the GUI."""
        timestamp = time.strftime('%H:%M:%S')
        self.send_update('log', f"[{timestamp}] [{self.printer_name}] {message}")

    def send_update(self, command, data):
        """Puts an update message into the queue for the GUI."""
        if self.is_running:
            self.update_queue.put((command, data))

    def send_status_update(self):
        """Sends this printer's current status to the GUI."""
        job_snapshot = self.current_job.get_snapshot() if self.current_job else None
        
        # --- MODIFICATION: Determine status dynamically ---
        current_status = "Idle" # Default
        if self.current_job:
            current_status = self.current_job.status # "Printing" or "Paused"
        elif self.is_paused.is_set():
            current_status = "Paused" # Paused but no job
        
        printer_info = {
            "id": self.printer_id,
            "name": self.printer_name,
            "job": job_snapshot,
            "status": current_status # <-- FIX
        }
        self.send_update('printer_status', printer_info)

    def run(self):
        """Main loop for the printer thread."""
        while not self.shutdown_flag.is_set():
            try:
                self.run_cycle()
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.current_job = None 
        
        if self.current_job:
            self.log(f"Shutting down. Re-queuing {self.current_job.file_name}.")
            self.current_job.status = "Queued"
            self.manager.add_job_to_queue(self.current_job, preempted=True)

        self.log("Offline.")
        self.is_running = False
        offline_status = {
            "id": self.printer_id,
            "name": self.printer_name,
            "job": None,
            "status": "Offline"
        }
        self.send_update('printer_status', offline_status)


    def run_cycle(self):
        """A single cycle of getting and processing a job."""
        if self.is_paused.is_set():
            if self.current_job:
                self.current_job.status = "Paused"
            self.send_status_update() 
            time.sleep(0.5)
            return

        if not self.current_job:
            self.send_status_update() # Report Idle
            if self.shutdown_flag.is_set():
                return
            
            self.current_job = self.manager.get_next_job(self.printer_id)
            
            if self.current_job:
                self.current_job.status = "Printing"
                self.log(f"Started: {self.current_job.file_name}")
            else:
                time.sleep(0.2) # No job, wait
                return 
        
        self.send_status_update() # Report Printing
        
        time.sleep(0.5) # Simulate printing one page
        
        if self.is_paused.is_set() or self.shutdown_flag.is_set():
            return

        self.current_job.progress += 1
        self.current_job.pages_remaining -= 1

        with self.interrupt_lock:
            if self.cancel_current_job:
                self.log(f"Canceled: {self.current_job.file_name}")
                self.current_job.status = "Canceled" 
                self.current_job = None
                self.cancel_current_job = False
                self.send_status_update() 
                return 

            if self.preempt_with_job:
                self.log(f"PREEMPTED by {self.preempt_with_job.file_name}.")
                self.current_job.status = "Queued (Preempted)"
                self.manager.add_job_to_queue(self.current_job, preempted=True)
                
                self.current_job = self.preempt_with_job
                self.current_job.status = "Printing"
                self.preempt_with_job = None
                return 

        if self.current_job.progress >= self.current_job.pages:
            self.log(f"Finished: {self.current_job.file_name}")
            self.current_job.status = "Completed"
            self.current_job = None
            self.send_status_update() 
            
    def toggle_pause(self):
        """Toggles the pause state of this printer."""
        if self.is_paused.is_set():
            self.is_paused.clear()
            if self.current_job:
                 self.current_job.status = "Printing"
            self.log("Resumed.")
        else:
            self.is_paused.set()
            if self.current_job:
                self.current_job.status = "Paused"
            self.log("Paused.")
        self.send_status_update()

    def stop(self):
        """Signals the printer to shut down gracefully after its current page."""
        self.log("Shutdown signal received...")
        self.shutdown_flag.set()

    def set_cancel_flag(self):
        """Sets a flag to cancel the current job."""
        with self.interrupt_lock:
            self.cancel_current_job = True

    def preempt(self, new_job):
        """Tells the printer to preempt its current job with a new one."""
        with self.interrupt_lock:
            self.preempt_with_job = new_job


# --- Central Job Manager (Not a thread) ---
class JobManager:
    """Manages the central job queue and dispatches to printers."""
    
    def __init__(self, update_queue):
        self.job_queue = deque()
        self.lock = threading.RLock()
        self.update_queue = update_queue
        self.printers = {} # Store printers as dict {id: instance}
        self.algorithm = "FCFS" # Default

    def add_printer(self, printer_instance):
        with self.lock:
            self.printers[printer_instance.printer_id] = printer_instance
        self.log(f"Printer '{printer_instance.printer_name}' (ID: {printer_instance.printer_id}) is online.")

    def remove_printer(self, printer_id):
        with self.lock:
            if printer_id in self.printers:
                printer = self.printers.pop(printer_id)
                printer.stop()
                self.log(f"Printer '{printer.printer_name}' (ID: {printer.printer_id}) is shutting down.")
            
    def get_printer_list(self):
        with self.lock:
            return list(self.printers.values())

    def auto_select_algorithm(self):
        """Auto-selects an algorithm based on queue state."""
        with self.lock:
            queue_length = len(self.job_queue)
            
            if queue_length == 0:
                new_algo = "FCFS"
            elif queue_length <= 3:
                new_algo = "FCFS"
            else:
                try:
                    avg_pages = sum(j.pages_remaining for j in self.job_queue) / queue_length
                    if avg_pages <= 20: 
                        new_algo = "SRTF"
                    else: 
                        new_algo = "SJF"
                except ZeroDivisionError:
                    new_algo = "FCFS" 

            if self.algorithm != new_algo:
                self.algorithm = new_algo
                self.log(f"Algorithm auto-switched to: {self.algorithm}")
                self.send_update('algorithm', self.algorithm)

    def send_update(self, command, data):
        """Puts an update message into the queue for the GUI."""
        self.update_queue.put((command, data))

    def log(self, message):
        timestamp = time.strftime('%H:%M:%S')
        self.send_update('log', f"[{timestamp}] [Manager] {message}")

    def add_job_to_queue(self, job, preempted=False):
        """Adds a job to the central queue."""
        with self.lock:
            if preempted:
                self.job_queue.appendleft(job) 
                self.log(f"Re-queued (Preempted): {job.file_name}")
            else:
                self.job_queue.append(job)
                self.log(f"Added: {job.file_name} ({job.pages}p) to queue.")

            if self.algorithm == "SRTF" and not preempted:
                self.check_for_preemption(job)
            
            self.auto_select_algorithm() 
        
        self.send_full_update()


    def check_for_preemption(self, new_job):
        """Checks if the new job should preempt any running job."""
        with self.lock:
            best_target_printer = None
            max_remaining_time = new_job.pages_remaining 
            
            for printer in self.printers.values():
                if printer.current_job and not printer.is_paused.is_set():
                    remaining_time = printer.current_job.pages_remaining
                    if remaining_time > max_remaining_time:
                        max_remaining_time = remaining_time
                        best_target_printer = printer
            
            if best_target_printer:
                self.log(f"SRTF PREEMPTION: Job {new_job.job_id} ({new_job.pages_remaining}p) is shorter.")
                try:
                    self.job_queue.remove(new_job) 
                    best_target_printer.preempt(new_job) 
                except ValueError:
                    self.log(f"SRTF INFO: Job {new_job.job_id} already taken.")


    def get_next_job(self, printer_id):
        """Selects the next job based on the current algorithm."""
        with self.lock:
            self.auto_select_algorithm()

            if not self.job_queue:
                return None
            
            next_job = None
            try:
                if self.algorithm == "FCFS":
                    next_job = self.job_queue.popleft()
                
                elif self.algorithm == "SJF" or self.algorithm == "SRTF":
                    sorted_jobs = sorted(list(self.job_queue), key=lambda j: j.pages_remaining)
                    next_job = sorted_jobs[0]
                    self.job_queue.remove(next_job)
                
                if next_job:
                    self.send_full_update() 
            
            except Exception as e:
                self.log(f"Error in get_next_job: {e}")
                return None
        
        return next_job


    def cancel_job(self, job_id):
        """Cancels a job, whether in the main queue or on a printer."""
        with self.lock:
            job_to_cancel = next((j for j in self.job_queue if j.job_id == job_id), None)
            if job_to_cancel:
                self.job_queue.remove(job_to_cancel)
                self.log(f"Canceled: Removed {job_to_cancel.file_name} from main queue.")
                self.send_full_update()
                return

            for printer in self.printers.values():
                if printer.current_job and printer.current_job.job_id == job_id:
                    self.log(f"Signaling Printer {printer.printer_name} to cancel {printer.current_job.file_name}.")
                    printer.set_cancel_flag()
                    return
        
        self.log(f"Could not find job {job_id} to cancel.")

    def send_full_update(self):
        """Sends a snapshot of the current queue state to the GUI."""
        with self.lock:
            queue_snapshot = [job.get_snapshot() for job in self.job_queue]
        self.send_update('update_queue', queue_snapshot)


# --- MODIFICATION: Reverted to BluetoothScanner class ---
class BluetoothScanner(tk.Toplevel):
    """A modal popup to simulate scanning for Bluetooth printers."""
    
    def __init__(self, parent, connected_printers):
        super().__init__(parent)
        self.parent = parent
        self.title("Simulated Bluetooth Scanner")
        self.geometry("400x300")
        self.transient(parent) # Keep on top
        self.grab_set() # Modal
        
        # Simulated list of all discoverable printers
        self.all_printers = {
            "HP_LaserJet_BT": "00:1A:7D:DA:71:13",
            "Epson_WF_BT": "08:00:4E:8B:0A:42",
            "Canon_Pixma_BT": "BC:F1:C3:7E:21:A1",
            "Brother_MFC_BT": "00:80:92:1A:BC:DE"
        }
        self.connected_printers = connected_printers # {id: name}
        
        self.init_ui()

    def init_ui(self):
        main_frame = ttk.Frame(self, padding=10, style="TFrame")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Use parent's styles
        self.style = self.parent.style
        self.configure(background=self.parent.BG_LIGHT)

        list_frame = ttk.Frame(main_frame, style="Card.TFrame", padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        ttk.Label(list_frame, text="Discovered Printers:", style="Card.TLabel").pack(anchor="w", pady=(0, 5))
        
        self.scan_listbox = tk.Listbox(list_frame, 
                                          background=self.parent.BG_WHITE, 
                                          foreground=self.parent.TEXT_DARK,
                                          selectbackground=self.parent.TREE_SELECT_BG,
                                          selectforeground=self.parent.TEXT_DARK,
                                          font=("Segoe UI", 10), 
                                          selectmode=tk.MULTIPLE,
                                          borderwidth=0,
                                          highlightthickness=0)
        self.scan_listbox.pack(fill=tk.BOTH, expand=True)
        
        button_frame = ttk.Frame(main_frame, style="TFrame")
        button_frame.pack(fill=tk.X)
        
        self.scan_button = ttk.Button(button_frame, text="Scan", command=self.scan_for_devices)
        self.scan_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))
        
        self.connect_button = ttk.Button(button_frame, text="Connect Selected", command=self.connect, style="Success.TButton", state="disabled")
        self.connect_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

        self.disconnect_button = ttk.Button(button_frame, text="Disconnect Selected", command=self.disconnect, style="Danger.TButton", state="disabled")
        self.disconnect_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))
        
        self.scan_listbox.bind('<<ListboxSelect>>', self.on_selection_change)

    def scan_for_devices(self):
        """Simulates scanning for devices."""
        self.scan_listbox.delete(0, tk.END)
        self.scan_button.config(text="Scanning...", state="disabled")
        self.connect_button.config(state="disabled")
        self.disconnect_button.config(state="disabled")
        
        self.after(1000, self.finish_scan) # Simulate 1 second scan

    def finish_scan(self):
        for name, mac in self.all_printers.items():
            display_name = f"{name} ({mac})"
            self.scan_listbox.insert(tk.END, display_name)
            
            # Check if this printer is already connected
            if name in self.connected_printers.values():
                self.scan_listbox.itemconfig(tk.END, {'bg': '#d4edda', 'fg': '#155724'}) # Greenish
        
        self.scan_button.config(text="Scan", state="normal")

    def on_selection_change(self, event=None):
        """Enable/disable buttons based on selection."""
        if not self.scan_listbox.curselection():
            self.connect_button.config(state="disabled")
            self.disconnect_button.config(state="disabled")
            return
        
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="normal")
        
    def connect(self):
        """Connects the selected printers."""
        selections = self.scan_listbox.curselection()
        if not selections:
            return
            
        for i in selections:
            full_name = self.scan_listbox.get(i)
            name_only = full_name.split(" (")[0]
            
            # Only add if not already connected
            if name_only not in self.connected_printers.values():
                self.parent.add_printer(name_only)
        
        self.destroy() # Close window

    def disconnect(self):
        """Disconnects the selected printers."""
        selections = self.scan_listbox.curselection()
        if not selections:
            return

        for i in selections:
            full_name = self.scan_listbox.get(i)
            name_only = full_name.split(" (")[0]
            
            # Find the ID of the printer with this name
            printer_id_to_remove = None
            for pid, pname in self.connected_printers.items():
                if pname == name_only:
                    printer_id_to_remove = pid
                    break
            
            if printer_id_to_remove:
                self.parent.remove_printer(printer_id_to_remove)
        
        self.destroy() # Close window


# --- GUI Application ---
class PrinterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Automatic Printer Scheduler") 
        self.geometry("1200x800") 
        self.resizable(True, True)
        
        self.themes = {
            "Light": {
                "BG_LIGHT": "#f8f9fa",
                "BG_WHITE": "#ffffff",
                "TEXT_DARK": "#212529",
                "TEXT_MUTED": "#555555",
                "BORDER_LIGHT": "#dee2e6",
                "PRIMARY_BLUE": "#007bff",
                "SUCCESS_GREEN": "#28a745",
                "WARN_ORANGE": "#fd7e14",
                "DANGER_RED": "#dc3545",
                "LOG_BG": "#e9e9e9",
                "TREE_SELECT_BG": "#cce5ff",
                "TREE_HEAD_BG": "#e9ecef"
            },
            "Dark": {
                "BG_LIGHT": "#212529",
                "BG_WHITE": "#343a40",
                "TEXT_DARK": "#f8f9fa",
                "TEXT_MUTED": "#adb5bd",
                "BORDER_LIGHT": "#495057",
                "PRIMARY_BLUE": "#0d6efd",
                "SUCCESS_GREEN": "#198754",
                "WARN_ORANGE": "#fd7e14",
                "DANGER_RED": "#dc3545",
                "LOG_BG": "#343a40",
                "TREE_SELECT_BG": "#0d6efd",
                "TREE_HEAD_BG": "#212529"
            }
        }
        
        self.update_queue = Queue() 
        self.job_manager = JobManager(self.update_queue)
        
        self.printers = {} # Store as {id: instance}
        self.printer_status_data = {} # Store as {id: status_dict}
        self.next_printer_id = 1

        self.set_theme("Light") 
        self.init_ui()

        self.process_updates()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        if not PYPDF2_AVAILABLE:
            self.log_message("WARNING: 'PyPDF2' not found. Install with 'pip install PyPDF2'.", "WARNING")
        else:
            self.log_message("INFO: 'PyPDF2' loaded successfully.", "INFO")
        
        # Add 1 printer by default
        self.add_printer("Default_Printer_1")

        self.queue_tree.bind('<<TreeviewSelect>>', self.update_button_states)
        self.printer_tree.bind('<<TreeviewSelect>>', self.update_button_states)

    def set_theme(self, theme_name):
        """Applies the selected color theme to the application."""
        theme = self.themes.get(theme_name, "Light")
        
        self.BG_LIGHT = theme["BG_LIGHT"]
        self.BG_WHITE = theme["BG_WHITE"]
        self.TEXT_DARK = theme["TEXT_DARK"]
        self.TEXT_MUTED = theme["TEXT_MUTED"]
        self.BORDER_LIGHT = theme["BORDER_LIGHT"]
        self.PRIMARY_BLUE = theme["PRIMARY_BLUE"]
        self.SUCCESS_GREEN = theme["SUCCESS_GREEN"]
        self.WARN_ORANGE = theme["WARN_ORANGE"]
        self.DANGER_RED = theme["DANGER_RED"]
        self.LOG_BG = theme["LOG_BG"]
        self.TREE_SELECT_BG = theme["TREE_SELECT_BG"]
        self.TREE_HEAD_BG = theme["TREE_HEAD_BG"]

        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.init_style()

        self.configure(background=self.BG_LIGHT)

        if hasattr(self, 'log_text'):
            self.log_text.config(background=self.LOG_BG, foreground=self.TEXT_DARK)
            self.log_text.tag_configure("ERROR", foreground=self.DANGER_RED)
            self.log_text.tag_configure("WARNING", foreground=self.WARN_ORANGE)
            self.log_text.tag_configure("INFO", foreground=self.SUCCESS_GREEN)
            self.log_text.tag_configure("HEADING", foreground=self.PRIMARY_BLUE, font=("Consolas", 9, "bold"))
            self.log_text.tag_configure("MUTED", foreground=self.TEXT_MUTED)
        
        if hasattr(self, 'queue_tree'):
            self.queue_tree.tag_configure("Queued", foreground=self.TEXT_MUTED)
            self.queue_tree.tag_configure("Queued (Preempted)", foreground=self.WARN_ORANGE, font=("Segoe UI", 10, 'italic'))
            self.queue_tree.tag_configure("Canceled", foreground=self.DANGER_RED, font=("Segoe UI", 10, 'italic'))
        
        if hasattr(self, 'printer_tree'):
            self.printer_tree.tag_configure("Idle", foreground=self.TEXT_MUTED)
            self.printer_tree.tag_configure("Printing", foreground=self.PRIMARY_BLUE, font=("Segoe UI", 10, 'bold'))
            self.printer_tree.tag_configure("Paused", foreground=self.WARN_ORANGE)
            self.printer_tree.tag_configure("Offline", foreground=self.DANGER_RED, font=("Segoe UI", 10, 'italic'))

    def init_style(self):
        self.style.configure(".", background=self.BG_LIGHT, foreground=self.TEXT_DARK, font=("Segoe UI", 10))
        self.style.configure("TFrame", background=self.BG_LIGHT)
        self.style.configure("TLabel", background=self.BG_LIGHT, foreground=self.TEXT_DARK)
        
        self.style.configure("Card.TFrame", 
                             background=self.BG_WHITE, 
                             borderwidth=1, 
                             relief="solid", 
                             bordercolor=self.BORDER_LIGHT)
        
        self.style.configure("Card.TLabel", 
                             background=self.BG_WHITE, 
                             foreground=self.PRIMARY_BLUE, 
                             font=("Segoe UI", 13, "bold"))
        
        self.style.configure("Inner.TLabel", background=self.BG_WHITE, foreground=self.TEXT_DARK)
        
        self.style.configure("TButton", 
                             font=("Segoe UI", 10, "bold"), 
                             padding=8,
                             background=self.PRIMARY_BLUE,
                             foreground="#ffffff", 
                             relief='flat',
                             borderwidth=0)
        self.style.map("TButton", 
                       background=[('active', '#0056b3'), ('hover', '#0069d9')],
                       relief=[('pressed', 'flat')])
        
        self.style.configure("Disabled.TButton",
                             background="#c0c0c0",
                             foreground="#808080")
        
        self.style.configure("Success.TButton", 
                             background=self.SUCCESS_GREEN,
                             foreground="#ffffff")
        self.style.map("Success.TButton", 
                       background=[('active', '#218838'), ('hover', '#218838')])
        
        self.style.configure("Warn.TButton", 
                             background=self.WARN_ORANGE,
                             foreground="#ffffff")
        self.style.map("Warn.TButton", 
                       background=[('active', '#d96c12'), ('hover', '#ff8d2d')])

        self.style.configure("Danger.TButton", 
                             background=self.DANGER_RED,
                             foreground="#ffffff")
        self.style.map("Danger.TButton", 
                       background=[('active', '#b02a2a'), ('hover', '#e74c3c')])
        
        self.style.configure("TEntry", 
                             fieldbackground=self.BG_WHITE, 
                             foreground=self.TEXT_DARK,
                             borderwidth=1, 
                             relief='solid', 
                             bordercolor=self.BORDER_LIGHT)
        
        self.style.configure("TSpinbox",
                             fieldbackground=self.BG_WHITE,
                             foreground=self.TEXT_DARK,
                             borderwidth=1,
                             relief='solid',
                             bordercolor=self.BORDER_LIGHT,
                             arrowcolor=self.PRIMARY_BLUE,
                             font=("Segoe UI", 10))
        
        self.style.configure("TCombobox",
                             fieldbackground=self.BG_WHITE,
                             foreground=self.TEXT_DARK,
                             borderwidth=1,
                             relief='solid',
                             bordercolor=self.BORDER_LIGHT,
                             arrowcolor=self.PRIMARY_BLUE)
        self.style.map('TCombobox', 
                       fieldbackground=[('readonly', self.BG_WHITE)],
                       selectbackground=[('readonly', self.BG_WHITE)],
                       selectforeground=[('readonly', self.TEXT_DARK)])

        
        self.style.configure("Treeview.Heading", 
                             background=self.TREE_HEAD_BG, 
                             foreground=self.TEXT_DARK, 
                             font=("Segoe UI", 10, "bold"), 
                             relief='flat', 
                             borderwidth=0)
        
        self.style.configure("Treeview", 
                             background=self.BG_WHITE, 
                             fieldbackground=self.BG_WHITE, 
                             foreground=self.TEXT_DARK,
                             rowheight=28)
        self.style.map("Treeview", 
                       background=[('selected', self.TREE_SELECT_BG)], 
                       foreground=[('selected', self.TEXT_DARK)])

        self.style.configure("StatusAlgo.TLabel", 
                             background=self.BG_WHITE, 
                             foreground=self.TEXT_MUTED, 
                             font=("Segoe UI", 10, "italic"))


    def init_ui(self):
        main_frame = ttk.Frame(self, padding="15", style="TFrame")
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.rowconfigure(1, weight=1) 
        main_frame.columnconfigure(0, weight=2) 
        main_frame.columnconfigure(1, weight=1) 

        config_card = ttk.Frame(main_frame, style="Card.TFrame", padding=15)
        config_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 15))
        
        ttk.Label(config_card, text="⚙️ Scheduler Config", style="Card.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        
        # --- MODIFICATION: Button text and command reverted ---
        self.manage_printers_button = ttk.Button(config_card, text="Manage Printers...", command=self.open_bluetooth_scanner)
        self.manage_printers_button.grid(row=1, column=0, sticky="w", padx=5)

        self.printer_count_label = ttk.Label(config_card, text="Connected Printers: 1", style="Inner.TLabel")
        self.printer_count_label.grid(row=1, column=1, sticky="w", padx=5)
        
        self.algo_status_label = ttk.Label(config_card, text="Active Algorithm: FCFS", style="StatusAlgo.TLabel")
        self.algo_status_label.grid(row=1, column=2, sticky="w", padx=(20, 5))
        
        self.theme_var = tk.StringVar(value="Light")
        self.theme_combo = ttk.Combobox(config_card, textvariable=self.theme_var, values=["Light", "Dark"], width=7, state="readonly")
        self.theme_combo.grid(row=1, column=3, sticky="e", padx=(20, 5))
        self.theme_combo.bind("<<ComboboxSelected>>", lambda e: self.set_theme(self.theme_var.get()))

        config_card.columnconfigure(3, weight=1) 

        # --- Left Column (Unchanged) ---
        left_column = ttk.Frame(main_frame, style="TFrame")
        left_column.grid(row=1, column=0, sticky="nsew", padx=(0, 15))
        left_column.rowconfigure(1, weight=1)
        left_column.columnconfigure(0, weight=1)

        input_card = ttk.Frame(left_column, style="Card.TFrame", padding=15)
        input_card.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        input_card.columnconfigure(1, weight=1)
        
        ttk.Label(input_card, text="➕ Add New Print Job", style="Card.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(input_card, text="PDF File:", style="Inner.TLabel").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.file_path_var = tk.StringVar()
        self.file_path_entry = ttk.Entry(input_card, textvariable=self.file_path_var, state="readonly")
        self.file_path_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        self.browse_button = ttk.Button(input_card, text="Browse...", command=self.select_pdf)
        self.browse_button.grid(row=1, column=3, padx=5, pady=5)
        ttk.Label(input_card, text="Pages:", style="Inner.TLabel").grid(row=2, column=0, padx=5, pady=5, sticky="w")
        self.pages_var = tk.StringVar()
        self.pages_entry = ttk.Entry(input_card, textvariable=self.pages_var, width=10)
        self.pages_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w")
        self.add_job_button = ttk.Button(input_card, text="Add to Queue", command=self.add_job)
        self.add_job_button.grid(row=3, column=0, columnspan=4, padx=5, pady=(10, 0), ipady=5, sticky="ew")
        input_card.columnconfigure(1, weight=1)

        queue_card = ttk.Frame(left_column, style="Card.TFrame", padding=15)
        queue_card.grid(row=1, column=0, sticky="nsew")
        queue_card.rowconfigure(1, weight=1)
        queue_card.columnconfigure(0, weight=1)
        ttk.Label(queue_card, text="📋 Print Queue", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.queue_tree = ttk.Treeview(queue_card, columns=("ID", "File", "Pages", "Remaining", "Status"), show="headings")
        self.queue_tree.heading("ID", text="ID")
        self.queue_tree.heading("File", text="File Name")
        self.queue_tree.heading("Pages", text="Pages")
        self.queue_tree.heading("Remaining", text="Left")
        self.queue_tree.heading("Status", text="Status")
        self.queue_tree.column("ID", width=70, anchor=tk.W, stretch=tk.NO)
        self.queue_tree.column("File", width=250, anchor=tk.W, stretch=tk.YES)
        self.queue_tree.column("Pages", width=60, anchor=tk.CENTER, stretch=tk.NO)
        self.queue_tree.column("Remaining", width=60, anchor=tk.CENTER, stretch=tk.NO)
        self.queue_tree.column("Status", width=100, anchor=tk.W, stretch=tk.NO)
        self.queue_tree.grid(row=1, column=0, sticky="nsew")

        # --- Right Column ---
        right_column = ttk.Frame(main_frame, style="TFrame")
        right_column.grid(row=1, column=1, sticky="nsew")
        right_column.rowconfigure(0, weight=1) 
        right_column.rowconfigure(2, weight=1) 
        right_column.columnconfigure(0, weight=1)

        printer_status_card = ttk.Frame(right_column, style="Card.TFrame", padding=15)
        printer_status_card.grid(row=0, column=0, sticky="nsew", pady=(0, 15))
        printer_status_card.rowconfigure(1, weight=1)
        printer_status_card.columnconfigure(0, weight=1)
        ttk.Label(printer_status_card, text="🖨️ Printer Status", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        
        self.printer_tree = ttk.Treeview(printer_status_card, columns=("Name", "Status", "File", "Progress"), show="headings")
        self.printer_tree.heading("Name", text="Printer Name")
        self.printer_tree.heading("Status", text="Status")
        self.printer_tree.heading("File", text="File")
        self.printer_tree.heading("Progress", text="Progress")
        self.printer_tree.column("Name", width=120, anchor=tk.W, stretch=tk.YES)
        self.printer_tree.column("Status", width=80, anchor=tk.W, stretch=tk.NO)
        self.printer_tree.column("File", width=120, anchor=tk.W, stretch=tk.YES)
        self.printer_tree.column("Progress", width=100, anchor=tk.W, stretch=tk.NO)
        self.printer_tree.grid(row=1, column=0, sticky="nsew")

        controls_card = ttk.Frame(right_column, style="Card.TFrame", padding=15)
        controls_card.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        ttk.Label(controls_card, text="Controls", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.pause_button = ttk.Button(controls_card, text="⏸️ Pause Selected Printer", command=self.pause_printer, style="Warn.TButton")
        self.pause_button.grid(row=2, column=0, sticky="ew", pady=5, padx=5)
        self.cancel_button = ttk.Button(controls_card, text="❌ Cancel Selected Job", command=self.cancel_job, style="Danger.TButton")
        self.cancel_button.grid(row=3, column=0, sticky="ew", pady=5, padx=5)
        controls_card.columnconfigure(0, weight=1)

        log_card = ttk.Frame(right_column, style="Card.TFrame", padding=15)
        log_card.grid(row=2, column=0, sticky="nsew")
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        ttk.Label(log_card, text="📜 Event Log", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.log_text = tk.Text(log_card, font=("Consolas", 9), borderwidth=0, highlightthickness=0, state="disabled", wrap="word")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        
        self.set_theme(self.theme_var.get()) 
        self.update_button_states()


    def log_message(self, message, tag=None):
        """Helper to insert messages into the log Text widget."""
        self.log_text.config(state="normal")
        
        if not tag:
            if "ERROR" in message: tag = "ERROR"
            elif "WARNING" in message: tag = "WARNING"
            elif "INFO" in message: tag = "INFO"
            elif "---" in message: tag = "HEADING"
            elif "Shutting down" in message: tag = "WARNING"
            elif "Offline" in message: tag = "ERROR"
            else: tag = "MUTED" 
        
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.config(state="disabled")
        self.log_text.see(tk.END)


    # --- MODIFICATION: Renamed function ---
    def open_bluetooth_scanner(self):
        """Opens the simulated scanner window."""
        connected_printers = {pid: p.printer_name for pid, p in self.printers.items()}
        # --- MODIFICATION: Call reverted class name ---
        scanner_window = BluetoothScanner(self, connected_printers)
        self.wait_window(scanner_window) 

    def add_printer(self, printer_name):
        """Adds a new printer to the system."""
        printer_id = self.next_printer_id
        self.next_printer_id += 1
        
        self.log_message(f"INFO: Connecting to '{printer_name}' (ID: {printer_id})...", "INFO")
        printer = Printer(printer_id, printer_name, self.job_manager, self.update_queue)
        self.printers[printer_id] = printer
        self.job_manager.add_printer(printer)
        printer.start()
        
        self.update_printers_display()
        self.printer_count_label.config(text=f"Connected Printers: {len(self.printers)}")
        return printer_id # Return new ID
    
    def remove_printer(self, printer_id):
        """Removes a printer from the system."""
        if printer_id in self.printers:
            printer_to_remove = self.printers.pop(printer_id)
            self.job_manager.remove_printer(printer_id)
            
            if printer_id in self.printer_status_data:
                del self.printer_status_data[printer_id]
                
            self.update_printers_display()
            self.printer_count_label.config(text=f"Connected Printers: {len(self.printers)}")
        else:
            self.log_message(f"WARNING: Tried to remove non-existent printer ID {printer_id}", "WARNING")
            
    # --- (End of new functions) ---


    def select_pdf(self):
        filepaths = filedialog.askopenfilenames(
            title="Select one or more PDF files",
            filetypes=(("PDF Files", "*.pdf"), ("All files", "*.*"))
        )
        if not filepaths:
            return
        
        if len(filepaths) > 1:
            if not PYPDF2_AVAILABLE:
                messagebox.showerror("Feature Disabled", "Install PyPDF2 for batch adding.")
                return
            
            jobs_added_count = 0
            for filepath in filepaths:
                file_name = os.path.basename(filepath)
                try:
                    with open(filepath, "rb") as f:
                        reader = PyPDF2.PdfReader(f)
                        pages = len(reader.pages)
                        if pages <= 0: raise ValueError("0 pages")
                    job = PrintJob(file_name, filepath, pages)
                    self.job_manager.add_job_to_queue(job) 
                    jobs_added_count += 1
                except Exception as e:
                    self.log_message(f"ERROR: Failed to add {file_name}: {e}", "ERROR")
            if jobs_added_count > 0:
                self.log_message(f"--- Added {jobs_added_count} jobs to queue. ---", "HEADING")
            
            self.file_path_var.set("")
            self.pages_var.set("")

        elif len(filepaths) == 1:
            filepath = filepaths[0]
            self.file_path_var.set(filepath)
            self.pages_entry.config(state="normal") 

            if PYPDF2_AVAILABLE:
                try:
                    with open(filepath, "rb") as f:
                        reader = PyPDF2.PdfReader(f)
                        self.pages_var.set(str(len(reader.pages)))
                except Exception as e:
                    self.pages_var.set("")
                    self.log_message(f"ERROR: Could not read PDF pages. Please enter manually.", "ERROR")
            else:
                self.pages_var.set("")

    def add_job(self):
        filepath = self.file_path_var.get()
        pages_str = self.pages_var.get()

        if not filepath and not pages_str:
            return 
        
        try:
            pages = int(pages_str)
            if not filepath or pages <= 0:
                raise ValueError("Invalid input")
        except ValueError:
            messagebox.showerror("Invalid Input", "Please select a file and enter a valid positive number for pages.")
            return
        
        job = PrintJob(os.path.basename(filepath), filepath, pages)
        self.job_manager.add_job_to_queue(job) 

        self.file_path_var.set("")
        self.pages_var.set("")
        self.pages_entry.config(state="normal") 
        self.queue_tree.focus_set() 


    def pause_printer(self):
        """Pauses the printer selected in the printer status treeview."""
        selected_items = self.printer_tree.selection()
        if not selected_items: return
        
        printer_id = selected_items[0]
        try:
            printer_id = int(printer_id)
        except (ValueError, IndexError):
            return 
        
        printer_to_pause = self.printers.get(printer_id)
        if printer_to_pause:
            printer_to_pause.toggle_pause()

    def cancel_job(self):
        """Cancels a job selected in EITHER treeview."""
        job_id_to_cancel = None
        
        selected_queue = self.queue_tree.selection()
        if selected_queue:
            try: job_id_to_cancel = self.queue_tree.item(selected_queue[0], "values")[0]
            except IndexError: pass 
        
        selected_printer = self.printer_tree.selection()
        if not job_id_to_cancel and selected_printer:
            try:
                printer_id = int(selected_printer[0])
                job_snapshot = self.printer_status_data.get(printer_id, {}).get('job')
                if job_snapshot: job_id_to_cancel = job_snapshot.job_id
            except (ValueError, IndexError): pass
        
        if not job_id_to_cancel: return
        
        if messagebox.askyesno("Confirm Cancel", f"Are you sure you want to cancel job {job_id_to_cancel}?"):
            self.job_manager.cancel_job(job_id_to_cancel)

    def update_button_states(self, event=None):
        """Enables/disables control buttons based on selection."""
        
        selected_printer_item = self.printer_tree.selection()
        selected_job_item = self.queue_tree.selection()

        if selected_printer_item:
            self.pause_button.config(state=tk.NORMAL, style="Warn.TButton")
        else:
            self.pause_button.config(state=tk.DISABLED, style="Disabled.TButton")

        if selected_printer_item or selected_job_item:
            self.cancel_button.config(state=tk.NORMAL, style="Danger.TButton")
        else:
            self.cancel_button.config(state=tk.DISABLED, style="Disabled.TButton")

    def process_updates(self):
        """Continuously checks the queue for updates from worker threads."""
        try:
            while True:
                command, data = self.update_queue.get_nowait()
                
                if command == 'log':
                    self.log_message(data)
                
                elif command == 'algorithm':
                    self.algo_status_label.config(text=f"Active Algorithm: {data}")
                
                elif command == 'update_queue':
                    self.update_queue_display(data)
                
                elif command == 'printer_status':
                    printer_info = data
                    printer_id = printer_info['id']
                    
                    if printer_info['status'] == "Offline":
                        # Handle offline printer removal
                        if printer_id in self.printers:
                             # This was a graceful shutdown
                             self.log_message(f"INFO: Printer '{printer_info['name']}' is now offline.", "INFO")
                             self.printers.pop(printer_id)
                             self.printer_count_label.config(text=f"Connected Printers: {len(self.printers)}")
                        if printer_id in self.printer_status_data:
                            self.printer_status_data.pop(printer_id)
                    else:
                        # Update status for an online printer
                        self.printer_status_data[printer_id] = printer_info
                    
                    self.update_printers_display()

        except Empty:
            pass # No updates
        finally:
            self.after(100, self.process_updates)

    def update_queue_display(self, queue_snapshot):
        """Refreshes the Queue Treeview with the current queue state."""
        selected_item = self.queue_tree.selection()
        
        self.queue_tree.delete(*self.queue_tree.get_children())
        for job in queue_snapshot:
            self.queue_tree.insert("", "end", iid=job.job_id, values=(
                job.job_id, job.file_name, job.pages, job.pages_remaining, job.status
            ), tags=(job.status,))
        
        if selected_item and self.queue_tree.exists(selected_item[0]):
            self.queue_tree.selection_set(selected_item[0])
            self.queue_tree.focus(selected_item[0])
        
        self.update_button_states() 

    def update_printers_display(self):
        """Refreshes the Printer Status Treeview."""
        selected_item = self.printer_tree.selection()

        self.printer_tree.delete(*self.printer_tree.get_children())
        
        for printer_id, printer_info in sorted(self.printer_status_data.items()):
            
            job = printer_info.get('job')
            status_str, file_str, progress_str = "Idle", "---", "---"
            
            if job:
                status_str = job.status
                file_str = job.file_name
                if job.pages > 0:
                    percent = (job.progress / job.pages) * 100
                    progress_str = f"{job.progress}/{job.pages} ({percent:.0f}%)"
                else:
                    progress_str = f"{job.progress}/{job.pages}"
            else:
                status_str = printer_info.get('status', 'Idle')
                if status_str != "Offline": status_str = "Idle" 

            self.printer_tree.insert("", "end", iid=printer_id, values=(
                printer_info['name'], status_str, file_str, progress_str
            ), tags=(status_str,))

        if selected_item and self.printer_tree.exists(selected_item[0]):
            self.printer_tree.selection_set(selected_item[0])
            self.printer_tree.focus(selected_item[0])

        self.update_button_states() 

    def on_closing(self):
        """Handles the window close event."""
        if messagebox.askokcancel("Quit", "Do you want to quit? This will stop all printing operations."):
            self.log_message("--- Shutting down all printers... ---", "HEADING")
            printer_ids = list(self.printers.keys())
            for pid in printer_ids:
                self.remove_printer(pid)
            
            self.after(500, self.destroy) 
            

if __name__ == "__main__":
    app = PrinterApp()
    app.mainloop()

