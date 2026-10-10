"""
Shutdown Timer
A small countdown utility that warns before shutting the PC down and lets
the user cancel in time. Stays on top of every other window until you
choose Cancel or Shutdown Now, or the timer reaches zero.
"""

import os
import tkinter as tk

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COUNTDOWN_SECONDS = 30
IS_WINDOWS = os.name == "nt"

COLORS = {
    "bg": "#161829",        # window background, deep indigo-navy
    "track": "#2a2e52",     # unfilled portion of the countdown ring
    "text": "#f5f3ff",      # primary text
    "muted": "#9090b3",     # secondary / caption text
    "safe": "#7c9fff",      # ring color when plenty of time remains
    "warn": "#ffd166",      # ring color at the halfway point
    "danger": "#ff5e5e",    # ring color (and Shutdown button) near zero
    "danger_hover": "#ff7a7a",
    "cancel": "#262a4d",
    "cancel_hover": "#343a6e",
}

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_SUBTITLE = ("Segoe UI", 10)
FONT_DIGITS = ("Segoe UI", 52, "bold")
FONT_CAPTION = ("Segoe UI", 10)
FONT_BUTTON = ("Segoe UI", 11, "bold")


# ---------------------------------------------------------------------------
# System actions
# ---------------------------------------------------------------------------
def run_shutdown():
    """Trigger an immediate system shutdown."""
    if IS_WINDOWS:
        os.system("shutdown /s /t 1")
    else:
        os.system("shutdown -h now")


def abort_shutdown():
    """Abort a pending OS-level shutdown, if one was issued."""
    if IS_WINDOWS:
        os.system("shutdown /a")


# ---------------------------------------------------------------------------
# Rounded, hover-aware button drawn on a Canvas. ttk buttons can't get real
# rounded corners (or reliable color control on Windows), so this gives full
# control over shape and hover state instead.
# ---------------------------------------------------------------------------
class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, bg, hover_bg, fg,
                 width=150, height=48, radius=14, font=FONT_BUTTON):
        super().__init__(parent, width=width, height=height,
                          bg=COLORS["bg"], highlightthickness=0, bd=0,
                          cursor="hand2")
        self.command = command
        self.bg = bg
        self.hover_bg = hover_bg
        self.shape = self._round_rect(2, 2, width - 2, height - 2, radius, fill=bg, outline="")
        self.create_text(width / 2, height / 2, text=text, fill=fg, font=font)
        self.bind("<Enter>", lambda _e: self.itemconfig(self.shape, fill=self.hover_bg))
        self.bind("<Leave>", lambda _e: self.itemconfig(self.shape, fill=self.bg))
        self.bind("<Button-1>", lambda _e: self.command())

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)


# ---------------------------------------------------------------------------
# Circular countdown indicator. The ring drains and shifts from blue, to
# amber, to red as time runs out, so urgency is communicated visually
# instead of relying on the number alone.
# ---------------------------------------------------------------------------
class CountdownRing(tk.Canvas):
    def __init__(self, parent, total_seconds, size=210, thickness=12):
        super().__init__(parent, width=size, height=size,
                          bg=COLORS["bg"], highlightthickness=0, bd=0)
        self.total = total_seconds
        pad = thickness
        self.create_oval(pad, pad, size - pad, size - pad,
                          outline=COLORS["track"], width=thickness)
        self.arc = self.create_arc(pad, pad, size - pad, size - pad,
                                    start=90, extent=-359.9,
                                    outline=COLORS["safe"], width=thickness, style=tk.ARC)
        self.number = self.create_text(size / 2, size / 2 - 6, text=str(total_seconds),
                                        fill=COLORS["text"], font=FONT_DIGITS)
        self.create_text(size / 2, size / 2 + 40, text="seconds left",
                          fill=COLORS["muted"], font=FONT_CAPTION)

    def update_value(self, seconds):
        seconds = max(seconds, 0)
        fraction = seconds / self.total
        extent = -359.9 if fraction >= 0.999 else -360 * fraction
        self.itemconfig(self.number, text=str(seconds))
        self.itemconfig(self.arc, extent=extent, outline=self._color_for(fraction))

    @staticmethod
    def _lerp(c1, c2, t):
        c1 = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        c2 = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        mixed = [int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)]
        return "#%02x%02x%02x" % tuple(mixed)

    def _color_for(self, fraction):
        if fraction > 0.5:
            t = 1 - (fraction - 0.5) / 0.5
            return self._lerp(COLORS["safe"], COLORS["warn"], t)
        t = 1 - fraction / 0.5
        return self._lerp(COLORS["warn"], COLORS["danger"], t)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
def main():
    root = tk.Tk()
    root.title("Shutdown Timer")
    root.configure(bg=COLORS["bg"])
    root.resizable(False, False)

    window_w, window_h = 380, 440
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x = (screen_w - window_w) // 2
    y = (screen_h - window_h) // 2
    root.geometry(f"{window_w}x{window_h}+{x}+{y}")

    # --- Always on top, and STAY there --------------------------------------
    # A single -topmost call can get knocked back by other apps that grab
    # focus, so we keep reasserting it on a timer for the life of the window.
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()

    def keep_on_top():
        try:
            root.attributes("-topmost", True)
            root.lift()
            root.after(500, keep_on_top)
        except tk.TclError:
            pass  # window has been closed, stop rescheduling

    keep_on_top()

    # --- Header --------------------------------------------------------------
    header = tk.Frame(root, bg=COLORS["bg"])
    header.pack(pady=(28, 6))
    tk.Label(header, text="System Shutdown", font=FONT_TITLE,
             bg=COLORS["bg"], fg=COLORS["text"]).pack()
    tk.Label(header, text="Your computer will shut down automatically.",
             font=FONT_SUBTITLE, bg=COLORS["bg"], fg=COLORS["muted"]).pack(pady=(2, 0))

    # --- Countdown ring --------------------------------------------------------
    ring = CountdownRing(root, COUNTDOWN_SECONDS)
    ring.pack(pady=22)

    # --- Buttons --------------------------------------------------------------
    button_row = tk.Frame(root, bg=COLORS["bg"])
    button_row.pack(pady=(10, 24))

    def on_cancel():
        abort_shutdown()
        root.destroy()

    def on_shutdown_now():
        run_shutdown()
        root.destroy()

    RoundedButton(button_row, "Cancel", on_cancel,
                  bg=COLORS["cancel"], hover_bg=COLORS["cancel_hover"],
                  fg=COLORS["text"], width=150, height=48).pack(side=tk.LEFT, padx=8)

    RoundedButton(button_row, "Shutdown Now", on_shutdown_now,
                  bg=COLORS["danger"], hover_bg=COLORS["danger_hover"],
                  fg="#1c1c1c", width=170, height=48).pack(side=tk.LEFT, padx=8)

    # --- Countdown logic --------------------------------------------------------
    remaining = COUNTDOWN_SECONDS

    def tick():
        nonlocal remaining
        try:
            remaining -= 1
            if remaining > 0:
                ring.update_value(remaining)
                root.after(1000, tick)
            else:
                ring.update_value(0)
                run_shutdown()
        except tk.TclError:
            pass  # window has been closed, stop the timer

    root.after(1000, tick)
    root.protocol("WM_DELETE_WINDOW", on_cancel)  # closing the window cancels, doesn't orphan a countdown
    root.mainloop()


if __name__ == "__main__":
    main()