import tkinter as tk
from tkinter import messagebox, ttk

class Process:
    def __init__(self, pid, arrival_time, burst_time, priority):
        self.pid = pid
        self.arrival_time = arrival_time
        self.burst_time = burst_time
        self.priority = priority
        self.remaining_time = burst_time
        self.waiting_time = 0
        self.turnaround_time = 0
        self.completion_time = 0

class SmartPrinterScheduler:
    def __init__(self, root):
        self.root = root
        self.root.title("Smart Printer Process Scheduler")
        self.root.geometry("700x500")
        self.root.configure(bg="#f0f0f0")

        self.process_list = []

        title = tk.Label(root, text="🖨️ Smart Printer Process Scheduler", font=("Arial", 18, "bold"), bg="#f0f0f0")
        title.pack(pady=10)

        frame = tk.Frame(root, bg="#f0f0f0")
        frame.pack(pady=10)

        tk.Label(frame, text="Process ID").grid(row=0, column=0, padx=10, pady=5)
        tk.Label(frame, text="Arrival Time").grid(row=0, column=1, padx=10, pady=5)
        tk.Label(frame, text="Burst Time").grid(row=0, column=2, padx=10, pady=5)
        tk.Label(frame, text="Priority").grid(row=0, column=3, padx=10, pady=5)

        self.pid_entry = tk.Entry(frame, width=10)
        self.arrival_entry = tk.Entry(frame, width=10)
        self.burst_entry = tk.Entry(frame, width=10)
        self.priority_entry = tk.Entry(frame, width=10)

        self.pid_entry.grid(row=1, column=0)
        self.arrival_entry.grid(row=1, column=1)
        self.burst_entry.grid(row=1, column=2)
        self.priority_entry.grid(row=1, column=3)

        add_button = tk.Button(frame, text="Add Process", command=self.add_process, bg="#4CAF50", fg="white")
        add_button.grid(row=1, column=4, padx=10)

        tk.Button(root, text="Run Scheduler", command=self.run_scheduler, bg="#2196F3", fg="white").pack(pady=10)

        self.tree = ttk.Treeview(root, columns=("PID", "Arrival", "Burst", "Priority", "CT", "TAT", "WT"), show='headings', height=10)
        for col in self.tree["columns"]:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=80, anchor="center")
        self.tree.pack(pady=10)

    def add_process(self):
        try:
            pid = self.pid_entry.get()
            at = int(self.arrival_entry.get())
            bt = int(self.burst_entry.get())
            pr = int(self.priority_entry.get())
            process = Process(pid, at, bt, pr)
            self.process_list.append(process)
            self.tree.insert("", "end", values=(pid, at, bt, pr, "-", "-", "-"))
            self.pid_entry.delete(0, tk.END)
            self.arrival_entry.delete(0, tk.END)
            self.burst_entry.delete(0, tk.END)
            self.priority_entry.delete(0, tk.END)
        except ValueError:
            messagebox.showerror("Error", "Please enter valid integer values!")

    def run_scheduler(self):
        if not self.process_list:
            messagebox.showwarning("Warning", "No processes added!")
            return

        self.process_list.sort(key=lambda x: x.arrival_time)
        total_waiting_time = 0
        total_turnaround_time = 0
        time = 0

        for p in self.process_list:
            if time < p.arrival_time:
                time = p.arrival_time
            time += p.burst_time
            p.completion_time = time
            p.turnaround_time = p.completion_time - p.arrival_time
            p.waiting_time = p.turnaround_time - p.burst_time
            total_waiting_time += p.waiting_time
            total_turnaround_time += p.turnaround_time

        avg_wt = total_waiting_time / len(self.process_list)
        avg_tat = total_turnaround_time / len(self.process_list)

        for i in self.tree.get_children():
            self.tree.delete(i)

        for p in self.process_list:
            self.tree.insert("", "end", values=(
                p.pid, p.arrival_time, p.burst_time, p.priority,
                p.completion_time, p.turnaround_time, p.waiting_time
            ))

        messagebox.showinfo("Result", f"Average Waiting Time: {avg_wt:.2f}\nAverage Turnaround Time: {avg_tat:.2f}")

if __name__ == "__main__":
    root = tk.Tk()
    app = SmartPrinterScheduler(root)
    root.mainloop()
