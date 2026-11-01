"""main.py - App Kivy: lanza el cálculo y abre el mapa en el navegador del móvil."""
import functools
import http.server
import threading
import webbrowser
from datetime import date, datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.utils import platform

import core

PORT = 8765
_srv = None

if platform == "android":
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    @run_on_ui_thread
    def set_awake(on):
        win = autoclass("org.kivy.android.PythonActivity").mActivity.getWindow()
        (win.addFlags if on else win.clearFlags)(128)  # FLAG_KEEP_SCREEN_ON
else:
    def set_awake(on):
        pass


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def start_server(directory):
    """Sirve la carpeta del mapa en 127.0.0.1 (permite geolocalización en el navegador)."""
    global _srv
    if _srv:
        return
    _srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT),
                                           functools.partial(_Quiet, directory=str(directory)))
    threading.Thread(target=_srv.serve_forever, daemon=True).start()


def open_url(url):
    if platform == "android":
        from jnius import autoclass
        Intent, Uri = autoclass("android.content.Intent"), autoclass("android.net.Uri")
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
    else:
        webbrowser.open(url)


class NiscaloApp(App):
    title = "Níscalos"

    def build(self):
        self.runner = core.Runner(self.user_data_dir, log=self.log)
        self.busy = False
        root = BoxLayout(orientation="vertical", padding=10, spacing=8)
        root.add_widget(Label(text="🍄 Níscalo suitability", size_hint_y=None, height=50, font_size="22sp"))

        row = GridLayout(cols=4, size_hint_y=None, height=50, spacing=6)
        self.toggles = {}
        for code, (_, name) in core.PROVINCES.items():
            b = ToggleButton(text=name, state="down")
            self.toggles[code] = b
            row.add_widget(b)
        root.add_widget(row)

        row2 = GridLayout(cols=2, size_hint_y=None, height=50, spacing=6)
        self.date_in = TextInput(text=date.today().isoformat(), multiline=False, hint_text="YYYY-MM-DD")
        self.n_in = TextInput(text="150", multiline=False, input_filter="int", hint_text="points/province")
        row2.add_widget(self.date_in)
        row2.add_widget(self.n_in)
        root.add_widget(row2)

        row3 = GridLayout(cols=2, size_hint_y=None, height=56, spacing=6)
        self.run_btn = Button(text="Run analysis", on_release=self.start)
        row3.add_widget(self.run_btn)
        row3.add_widget(Button(text="Open last map", on_release=lambda *_: self.open_map()))
        root.add_widget(row3)

        self.scroll = ScrollView()
        self.out = Label(text="Date and points/province above. First run downloads pine forests "
                              "and relief tiles (~60 MB); later runs are fast.\n",
                         size_hint_y=None, halign="left", valign="top")
        self.out.bind(width=lambda *_: setattr(self.out, "text_size", (self.out.width, None)),
                      texture_size=lambda i, s: setattr(i, "height", s[1]))
        self.scroll.add_widget(self.out)
        root.add_widget(self.scroll)
        return root

    def log(self, msg):
        def _u(_dt):
            self.out.text += msg + "\n"
            self.scroll.scroll_y = 0
        Clock.schedule_once(_u)

    def start(self, *_):
        if self.busy:
            return
        provs = [c for c, b in self.toggles.items() if b.state == "down"]
        try:
            target = datetime.strptime(self.date_in.text.strip(), "%Y-%m-%d").date()
            n = max(20, min(1000, int(self.n_in.text)))
        except ValueError:
            self.log("Check the date (YYYY-MM-DD) and the number of points.")
            return
        if not provs:
            self.log("Select at least one province.")
            return
        self.busy = True
        self.run_btn.text, self.run_btn.disabled = "Running...", True
        set_awake(True)
        threading.Thread(target=self._work, args=(provs, target, n), daemon=True).start()

    def _work(self, provs, target, n):
        try:
            self.runner.run(provs, target, per_province=n)
            Clock.schedule_once(lambda dt: self.open_map())
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR: {e}")
        finally:
            def _reset(_dt):
                self.busy = False
                self.run_btn.text, self.run_btn.disabled = "Run analysis", False
                set_awake(False)
            Clock.schedule_once(_reset)

    def open_map(self):
        if not (self.runner.out_dir / "index.html").exists():
            self.log("No map yet. Run the analysis first.")
            return
        start_server(self.runner.out_dir)
        open_url(f"http://127.0.0.1:{PORT}/index.html")


if __name__ == "__main__":
    NiscaloApp().run()
