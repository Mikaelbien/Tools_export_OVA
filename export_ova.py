"""Export de machines virtuelles en OVA/OVF avec VMware OVF Tool.

Sources prises en charge :
  - VMware Workstation / Player : fichier .vmx local
  - Serveur ESXi (ou vCenter)   : machine virtuelle de l'inventaire (vi://)

Interface graphique en tkinter (aucune dépendance externe).
Référence : documentation/VMware Ovf Tool.pdf
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

APP_TITLE = "Export VM -> OVA / OVF"

OVFTOOL_CANDIDATES = [
    r"C:\Program Files\VMware\VMware OVF Tool\ovftool.exe",
    r"C:\Program Files (x86)\VMware\VMware OVF Tool\ovftool.exe",
    r"C:\Program Files (x86)\VMware\VMware Workstation\OVFTool\ovftool.exe",
    r"C:\Program Files\VMware\VMware Workstation\OVFTool\ovftool.exe",
    "/usr/bin/ovftool",
    "/usr/lib/vmware-ovftool/ovftool",
]

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
LOCAL_OVFTOOL = os.path.join(
    APP_DIR, "ovftool",
    "ovftool.exe" if os.name == "nt" else "ovftool")


def find_ovftool():
    if os.path.isfile(LOCAL_OVFTOOL):
        return LOCAL_OVFTOOL
    path = shutil.which("ovftool")
    if path:
        return path
    for candidate in OVFTOOL_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


def vi_locator(host, user, password, path=""):
    """Construit un localisateur vi:// (caractères spéciaux encodés en %XX)."""
    cred = quote(user, safe="")
    if password:
        cred += ":" + quote(password, safe="")
    return f"vi://{cred}@{host}/{path}"


def mask_password(text, password):
    if not password:
        return text
    return text.replace(quote(password, safe=""), "****")


def run_ovftool(args, on_line=None, proc_holder=None):
    """Lance ovftool et renvoie (code retour, sortie complète).

    ovftool affiche sa progression avec des retours chariot (\\r) : la sortie
    est donc découpée sur \\r et \\n.
    """
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    if proc_holder is not None:
        proc_holder["proc"] = proc
    chunks, buf = [], b""
    while True:
        byte = proc.stdout.read(1)
        if not byte:
            break
        if byte in (b"\r", b"\n"):
            if buf:
                line = buf.decode("utf-8", "replace")
                chunks.append(line)
                if on_line:
                    on_line(line)
                buf = b""
        else:
            buf += byte
    if buf:
        line = buf.decode("utf-8", "replace")
        chunks.append(line)
        if on_line:
            on_line(line)
    proc.wait()
    return proc.returncode, "\n".join(chunks)


def list_children(ovftool, host, user, password, path, no_ssl_verify):
    """Liste les enfants d'un chemin d'inventaire vi://.

    ovftool répond à un chemin incomplet par une erreur contenant
    « Possible completions are: ».
    """
    args = [ovftool]
    if no_ssl_verify:
        args.append("--noSSLVerify")
    args.append(vi_locator(host, user, password, path))
    code, out = run_ovftool(args)
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "Possible completions" in line:
            return [entry.strip() for entry in lines[i + 1:] if entry.strip()]
    if "Authentication failed" in out or "Invalid login" in out:
        raise RuntimeError("Authentification refusée par le serveur.")
    raise RuntimeError(mask_password(out.strip() or f"ovftool code {code}", password))


def list_vms(ovftool, host, user, password, no_ssl_verify, max_depth=5):
    """Retourne la liste des chemins de VM (relatifs à la racine vi://)."""
    vms = []

    def walk(path, depth):
        if depth > max_depth:
            return
        for entry in list_children(ovftool, host, user, password, path, no_ssl_verify):
            child = path + entry
            if entry.endswith("/"):
                # Racine -> datacenters ; datacenter -> vm/ uniquement
                # (host/, network/, datastore/ sont ignorés) ; puis sous-dossiers de VM.
                in_vm_tree = "/vm/" in child
                is_datacenter = path == ""
                if is_datacenter or entry == "vm/" or in_vm_tree:
                    walk(child, depth + 1)
            elif "/vm/" in child:
                vms.append(child)

    walk("", 0)
    return sorted(vms)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x720")
        self.minsize(720, 620)

        self.events = queue.Queue()
        self.proc_holder = {}
        self.worker = None
        self.cancelled = False

        self.ovftool_var = tk.StringVar(value=find_ovftool())
        self.out_dir_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.format_var = tk.StringVar(value="ova")

        self.vmx_var = tk.StringVar()

        self.host_var = tk.StringVar()
        self.user_var = tk.StringVar(value="root")
        self.pass_var = tk.StringVar()
        self.vm_var = tk.StringVar()

        self.overwrite_var = tk.BooleanVar(value=False)
        self.compress_var = tk.IntVar(value=0)
        self.sha_var = tk.StringVar(value="sha256")
        self.no_ssl_var = tk.BooleanVar(value=True)
        self.power_off_var = tk.BooleanVar(value=False)
        self.eulas_var = tk.BooleanVar(value=True)

        self._build_ui()
        self.after(100, self._poll_events)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        tool = ttk.LabelFrame(self, text="OVF Tool")
        tool.pack(fill="x", **pad)
        ttk.Entry(tool, textvariable=self.ovftool_var).pack(
            side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(tool, text="Parcourir…", command=self._browse_ovftool).pack(
            side="left", padx=6)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="x", **pad)

        local = ttk.Frame(self.tabs)
        self.tabs.add(local, text="Workstation / Player (.vmx)")
        local.columnconfigure(1, weight=1)
        ttk.Label(local, text="Fichier .vmx :").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(local, textvariable=self.vmx_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(local, text="Parcourir…", command=self._browse_vmx).grid(row=0, column=2, **pad)
        ttk.Label(
            local,
            text="La VM doit être éteinte (pas suspendue) pour être exportée.",
            foreground="#666",
        ).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        esxi = ttk.Frame(self.tabs)
        self.tabs.add(esxi, text="Serveur ESXi / vCenter")
        esxi.columnconfigure(1, weight=1)
        ttk.Label(esxi, text="Serveur :").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(esxi, textvariable=self.host_var).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(esxi, text="Utilisateur :").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(esxi, textvariable=self.user_var).grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(esxi, text="Mot de passe :").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(esxi, textvariable=self.pass_var, show="•").grid(row=2, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(esxi, text="Machine virtuelle :").grid(row=3, column=0, sticky="w", **pad)
        self.vm_combo = ttk.Combobox(esxi, textvariable=self.vm_var)
        self.vm_combo.grid(row=3, column=1, sticky="ew", **pad)
        self.list_btn = ttk.Button(esxi, text="Lister les VM", command=self._list_vms)
        self.list_btn.grid(row=3, column=2, **pad)
        ttk.Label(
            esxi,
            text="Chemin de la VM, ex. ha-datacenter/vm/MaVM (ESXi) ou Datacenter/vm/MaVM (vCenter).",
            foreground="#666",
        ).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        out = ttk.LabelFrame(self, text="Destination")
        out.pack(fill="x", **pad)
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Dossier :").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(out, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(out, text="Parcourir…", command=self._browse_out).grid(row=0, column=2, **pad)
        ttk.Label(out, text="Nom :").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(out, textvariable=self.name_var).grid(row=1, column=1, sticky="ew", **pad)
        fmt = ttk.Frame(out)
        fmt.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(out, text="Format :").grid(row=2, column=0, sticky="w", **pad)
        ttk.Radiobutton(fmt, text="OVA (fichier unique)", variable=self.format_var, value="ova").pack(side="left")
        ttk.Radiobutton(fmt, text="OVF (dossier + .vmdk)", variable=self.format_var, value="ovf").pack(side="left", padx=12)

        opts = ttk.LabelFrame(self, text="Options")
        opts.pack(fill="x", **pad)
        ttk.Checkbutton(opts, text="Écraser si existant (--overwrite)", variable=self.overwrite_var).grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Accepter les EULA (--acceptAllEulas)", variable=self.eulas_var).grid(row=0, column=1, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Ignorer le certificat SSL (ESXi, --noSSLVerify)", variable=self.no_ssl_var).grid(row=1, column=0, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Éteindre la VM source (ESXi, --powerOffSource)", variable=self.power_off_var).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(opts, text="Compression (0 = aucune, 1-9) :").grid(row=2, column=0, sticky="w", **pad)
        ttk.Spinbox(opts, from_=0, to=9, width=4, textvariable=self.compress_var).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(opts, text="Empreinte du manifeste :").grid(row=3, column=0, sticky="w", **pad)
        ttk.Combobox(opts, textvariable=self.sha_var, values=["sha1", "sha256", "sha512"],
                     state="readonly", width=8).grid(row=3, column=1, sticky="w", **pad)

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="Exporter", command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(actions, text="Annuler", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(actions, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.status_var = tk.StringVar(value="Prêt")
        ttk.Label(actions, textvariable=self.status_var, width=18).pack(side="left")

        logf = ttk.LabelFrame(self, text="Journal")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=10, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------ browsing
    def _browse_ovftool(self):
        path = filedialog.askopenfilename(
            title="Sélectionner ovftool", filetypes=[("ovftool", "ovftool*"), ("Tous", "*.*")])
        if path:
            self.ovftool_var.set(path)

    def _browse_vmx(self):
        path = filedialog.askopenfilename(
            title="Sélectionner une VM", filetypes=[("VMware VM", "*.vmx"), ("Tous", "*.*")])
        if path:
            self.vmx_var.set(os.path.normpath(path))
            if not self.name_var.get():
                self.name_var.set(os.path.splitext(os.path.basename(path))[0])

    def _browse_out(self):
        path = filedialog.askdirectory(title="Dossier de destination")
        if path:
            self.out_dir_var.set(os.path.normpath(path))

    # ------------------------------------------------------------- logging
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._log(value)
                    match = re.search(r"(\d{1,3})\s*%", value)
                    if match:
                        pct = min(int(match.group(1)), 100)
                        self.progress["value"] = pct
                        self.status_var.set(f"{pct} %")
                elif kind == "vms":
                    self.vm_combo["values"] = value
                    if value:
                        self.vm_var.set(value[0])
                    self._log(f"{len(value)} VM trouvée(s).")
                    self.list_btn.configure(state="normal")
                elif kind == "vms_error":
                    self._log(f"Erreur : {value}")
                    self.list_btn.configure(state="normal")
                    messagebox.showerror(APP_TITLE, value)
                elif kind == "done":
                    self._finish(*value)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    # ---------------------------------------------------------- validation
    def _check_ovftool(self):
        path = self.ovftool_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror(APP_TITLE, "ovftool introuvable : indiquez son chemin.")
            return None
        return path

    def _esxi_fields(self):
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        if not host or not user:
            messagebox.showerror(APP_TITLE, "Renseignez le serveur et l'utilisateur.")
            return None
        return host, user, self.pass_var.get()

    # ------------------------------------------------------------ VM list
    def _list_vms(self):
        ovftool = self._check_ovftool()
        fields = self._esxi_fields()
        if not ovftool or not fields:
            return
        host, user, password = fields
        self.list_btn.configure(state="disabled")
        self._log(f"Recherche des VM sur {host}…")

        def work():
            try:
                vms = list_vms(ovftool, host, user, password, self.no_ssl_var.get())
                self.events.put(("vms", vms))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("vms_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- export
    def _build_command(self, ovftool):
        """Construit la commande ; renvoie (args, cible) ou None si invalide."""
        is_esxi = self.tabs.index(self.tabs.select()) == 1
        out_dir = self.out_dir_var.get().strip()
        fmt = self.format_var.get()

        if is_esxi:
            fields = self._esxi_fields()
            vm_path = self.vm_var.get().strip().strip("/")
            if not fields or not vm_path:
                if fields:
                    messagebox.showerror(APP_TITLE, "Choisissez la machine virtuelle à exporter.")
                return None
            host, user, password = fields
            source = vi_locator(host, user, password, quote(vm_path, safe="/"))
            default_name = vm_path.rsplit("/", 1)[-1]
        else:
            vmx = self.vmx_var.get().strip()
            if not vmx or not os.path.isfile(vmx):
                messagebox.showerror(APP_TITLE, "Sélectionnez un fichier .vmx valide.")
                return None
            source = vmx
            default_name = os.path.splitext(os.path.basename(vmx))[0]

        if not out_dir:
            messagebox.showerror(APP_TITLE, "Choisissez le dossier de destination.")
            return None
        name = self.name_var.get().strip() or default_name
        if re.search(r'[\\/:*?"<>|]', name):
            messagebox.showerror(APP_TITLE, "Le nom contient des caractères interdits.")
            return None

        if fmt == "ova":
            target = os.path.join(out_dir, name + ".ova")
        else:
            target = os.path.join(out_dir, name, name + ".ovf")

        args = [ovftool, f"--targetType={fmt.upper()}", f"--name={name}",
                f"--shaAlgorithm={self.sha_var.get()}"]
        if self.overwrite_var.get():
            args.append("--overwrite")
        if self.eulas_var.get():
            args.append("--acceptAllEulas")
        if self.compress_var.get() > 0:
            args.append(f"--compress={self.compress_var.get()}")
        if is_esxi:
            if self.no_ssl_var.get():
                args.append("--noSSLVerify")
            if self.power_off_var.get():
                args.append("--powerOffSource")
        args += [source, target]
        password = self.pass_var.get() if is_esxi else ""
        return args, target, password

    def _start(self):
        ovftool = self._check_ovftool()
        if not ovftool:
            return
        built = self._build_command(ovftool)
        if not built:
            return
        args, target, password = built

        if os.path.exists(target) and not self.overwrite_var.get():
            messagebox.showerror(APP_TITLE, f"{target} existe déjà. Cochez « Écraser » pour le remplacer.")
            return
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de créer le dossier : {exc}")
            return

        self.cancelled = False
        self.progress["value"] = 0
        self.status_var.set("En cours…")
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self._log("> " + mask_password(subprocess.list2cmdline(args), password))

        def on_line(line):
            self.events.put(("log", mask_password(line, password)))

        def work():
            try:
                code, _ = run_ovftool(args, on_line, self.proc_holder)
            except OSError as exc:
                self.events.put(("log", f"Impossible de lancer ovftool : {exc}"))
                code = -1
            self.events.put(("done", (code, target)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self):
        proc = self.proc_holder.get("proc")
        if proc and proc.poll() is None:
            self.cancelled = True
            proc.terminate()
            self._log("Annulation demandée…")

    def _finish(self, code, target):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if self.cancelled:
            self.status_var.set("Annulé")
            self._log("Export annulé (le fichier partiel peut subsister).")
        elif code == 0:
            self.progress["value"] = 100
            self.status_var.set("Terminé")
            self._log(f"Export terminé : {target}")
            messagebox.showinfo(APP_TITLE, f"Export terminé :\n{target}")
        else:
            self.status_var.set("Échec")
            self._log(f"Échec de l'export (code {code}).")
            messagebox.showerror(APP_TITLE, f"Échec de l'export (code {code}). Voir le journal.")


if __name__ == "__main__":
    App().mainloop()
