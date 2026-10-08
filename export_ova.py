"""Export / import de machines virtuelles (OVA/OVF) avec VMware OVF Tool.

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
import ssl
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import base64
import html
import urllib.error
import urllib.request
from urllib.parse import quote, unquote

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


def list_children(ovftool, host, user, password, path, no_ssl_verify, log=None):
    """Liste les enfants d'un chemin d'inventaire vi://.

    ovftool répond à un chemin incomplet par une erreur contenant
    « Possible completions are: ».
    """
    args = [ovftool]
    if no_ssl_verify:
        args.append("--noSSLVerify")
    args.append(vi_locator(host, user, password, path))
    code, out = run_ovftool(args)
    if log:
        log(mask_password(f"[vi://{host}/{path}] code {code}\n{out}", password))
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "Possible completions" in line:
            return [entry.strip() for entry in lines[i + 1:] if entry.strip()]
    if "Authentication failed" in out or "Invalid login" in out:
        raise RuntimeError("Authentification refusée par le serveur.")
    raise RuntimeError(mask_password(out.strip() or f"ovftool code {code}", password))


def list_vms(ovftool, host, user, password, no_ssl_verify, max_depth=5, log=None):
    """Retourne la liste des chemins de VM (relatifs à la racine vi://)."""
    vms = []

    def walk(path, depth):
        if depth > max_depth:
            return
        for entry in list_children(ovftool, host, user, password, path, no_ssl_verify, log):
            child = path + entry
            if entry.endswith("/"):
                # Racine -> datacenters ; datacenter -> vm/ uniquement
                # (host/, network/, datastore/ sont ignorés) ; puis sous-dossiers de VM.
                in_vm_tree = "/vm/" in child
                is_datacenter = path == ""
                if is_datacenter or entry == "vm/" or in_vm_tree:
                    walk(child, depth + 1)
            else:
                # ESXi autonome : les VM sont listées directement à la racine
                # (pool de ressources) ; vCenter : sous <datacenter>/vm/.
                vms.append(child)

    walk("", 0)
    return sorted(vms)


def list_datastores(host, user, password, no_ssl_verify):
    """Liste les datastores via le navigateur de fichiers HTTPS (/folder) d'ESXi/vCenter."""
    ctx = ssl.create_default_context()
    if no_ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")

    def get(path):
        req = urllib.request.Request(
            f"https://{host}{path}", headers={"Authorization": f"Basic {token}"})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("Authentification refusée par le serveur.") from exc
            raise RuntimeError(f"Erreur HTTP {exc.code} sur {path}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"Connexion impossible à {host} : {exc}") from exc

    page = html.unescape(get("/folder"))
    datacenters = sorted(set(unquote(m) for m in re.findall(r"dcPath=([^\"'&<>\s]+)", page)))
    names = set()
    for dc in datacenters:
        page = html.unescape(get("/folder?dcPath=" + quote(dc, safe="")))
        for m in re.findall(r"dsName=([^\"'&<>\s]+)", page):
            names.add(unquote(m))
    return sorted(names)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("860x940")
        self.minsize(760, 760)

        self.events = queue.Queue()
        self.proc_holder = {}
        self.worker = None
        self.cancelled = False

        self.ovftool_var = tk.StringVar(value=find_ovftool())
        self.out_dir_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.format_var = tk.StringVar(value="ova")
        self.vmx_disk_var = tk.StringVar(value="(par défaut)")

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

        self.op_label = "Export"
        self.src_pkg_var = tk.StringVar()
        self.imp_target_var = tk.StringVar(value="esxi")
        self.imp_name_var = tk.StringVar()
        self.imp_datastore_var = tk.StringVar()
        self.imp_network_var = tk.StringVar()
        self.imp_disk_var = tk.StringVar(value="thin")
        self.imp_folder_var = tk.StringVar()
        self.imp_path_var = tk.StringVar()
        self.imp_out_var = tk.StringVar()
        self.imp_power_on_var = tk.BooleanVar(value=False)
        self.imp_overwrite_var = tk.BooleanVar(value=False)

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

        self.modes = ttk.Notebook(self)
        self.modes.pack(fill="x", **pad)
        export_page = ttk.Frame(self.modes)
        self.modes.add(export_page, text="Exporter (VM → OVA / OVF)")
        import_page = ttk.Frame(self.modes)
        self.modes.add(import_page, text="Importer / Déployer (OVA / OVF → VM)")
        self.modes.bind("<<NotebookTabChanged>>", self._mode_changed)

        self.tabs = ttk.Notebook(export_page)
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
            text="Nom de la VM (ESXi) ou chemin Datacenter/vm/MaVM (vCenter).",
            foreground="#666",
        ).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        out = ttk.LabelFrame(export_page, text="Destination")
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
        self.vmx_radio = ttk.Radiobutton(
            fmt, text="VMX (Workstation, depuis ESXi)", variable=self.format_var, value="vmx")
        self.vmx_radio.pack(side="left")
        self.vmx_disk_label = ttk.Label(out, text="Format des disques :")
        self.vmx_disk_label.grid(row=3, column=0, sticky="w", **pad)
        self.vmx_disk_combo = ttk.Combobox(
            out, textvariable=self.vmx_disk_var, state="readonly", width=22,
            values=["(par défaut)", "monolithicSparse", "twoGbMaxExtentSparse",
                    "monolithicFlat", "twoGbMaxExtentFlat"])
        self.vmx_disk_combo.grid(row=3, column=1, sticky="w", **pad)
        self.format_var.trace_add("write", lambda *_: self._format_changed())
        self._format_changed()

        opts = ttk.LabelFrame(export_page, text="Options")
        opts.pack(fill="x", **pad)
        ttk.Checkbutton(opts, text="Écraser si existant (--overwrite)", variable=self.overwrite_var).grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Accepter les EULA (--acceptAllEulas)", variable=self.eulas_var).grid(row=0, column=1, sticky="w", **pad)
        self.exp_esxi_opts = [
            ttk.Checkbutton(opts, text="Ignorer le certificat SSL (--noSSLVerify)", variable=self.no_ssl_var),
            ttk.Checkbutton(opts, text="Éteindre la VM source (--powerOffSource)", variable=self.power_off_var),
        ]
        self.exp_esxi_opts[0].grid(row=1, column=0, sticky="w", **pad)
        self.exp_esxi_opts[1].grid(row=1, column=1, sticky="w", **pad)
        self.tabs.bind("<<NotebookTabChanged>>", self._export_source_changed)
        ttk.Label(opts, text="Compression (0 = aucune, 1-9) :").grid(row=2, column=0, sticky="w", **pad)
        ttk.Spinbox(opts, from_=0, to=9, width=4, textvariable=self.compress_var).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(opts, text="Empreinte du manifeste :").grid(row=3, column=0, sticky="w", **pad)
        ttk.Combobox(opts, textvariable=self.sha_var, values=["sha1", "sha256", "sha512"],
                     state="readonly", width=8).grid(row=3, column=1, sticky="w", **pad)

        self._build_import_page(import_page)

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

    def _build_import_page(self, page):
        pad = {"padx": 8, "pady": 4}

        src = ttk.LabelFrame(page, text="Paquet à importer")
        src.pack(fill="x", **pad)
        src.columnconfigure(1, weight=1)
        ttk.Label(src, text="Fichier .ova / .ovf :").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(src, textvariable=self.src_pkg_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(src, text="Parcourir…", command=self._browse_pkg).grid(row=0, column=2, **pad)
        self.analyse_btn = ttk.Button(src, text="Analyser", command=self._analyse)
        self.analyse_btn.grid(row=0, column=3, **pad)

        tgt = ttk.LabelFrame(page, text="Cible")
        tgt.pack(fill="x", **pad)
        tgt.columnconfigure(1, weight=1)
        kind = ttk.Frame(tgt)
        kind.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        ttk.Radiobutton(kind, text="Serveur ESXi / vCenter",
                        variable=self.imp_target_var, value="esxi").pack(side="left")
        ttk.Radiobutton(kind, text="Workstation / Player (.vmx)",
                        variable=self.imp_target_var, value="vmx").pack(side="left", padx=12)

        self._imp_widgets = {"esxi": [], "vmx": []}

        def grid_row(r, group, label, widget_factory, hint=""):
            lab = ttk.Label(tgt, text=label)
            lab.grid(row=r, column=0, sticky="w", **pad)
            widget = widget_factory()
            widget.grid(row=r, column=1, sticky="ew", **pad)
            found = [lab, widget]
            if hint:
                h = ttk.Label(tgt, text=hint, foreground="#666")
                h.grid(row=r, column=2, sticky="w", **pad)
                found.append(h)
            if group:
                self._imp_widgets[group].extend(found)

        def entry(var, show=None):
            return lambda: ttk.Entry(tgt, textvariable=var, show=show) if show else ttk.Entry(tgt, textvariable=var)

        grid_row(1, "esxi", "Serveur :", entry(self.host_var))
        grid_row(2, "esxi", "Utilisateur :", entry(self.user_var))
        grid_row(3, "esxi", "Mot de passe :", entry(self.pass_var, show="•"))
        grid_row(4, None, "Nom de la VM :", entry(self.imp_name_var), "vide = nom du paquet")
        grid_row(5, "esxi", "Datastore :", lambda: self._make_ds_combo(tgt))
        self.ds_btn = ttk.Button(tgt, text="Lister", command=self._list_datastores)
        self.ds_btn.grid(row=5, column=2, sticky="w", **pad)
        self._imp_widgets["esxi"].append(self.ds_btn)
        grid_row(6, "esxi", "Réseau :", entry(self.imp_network_var), "--network")
        grid_row(7, "esxi", "Dossier VM :", entry(self.imp_folder_var), "--vmFolder (optionnel)")
        grid_row(8, "esxi", "Chemin cible :", entry(self.imp_path_var), "vCenter : Datacenter/host/Cluster")
        grid_row(9, "esxi", "Format des disques :", lambda: ttk.Combobox(
            tgt, textvariable=self.imp_disk_var, state="readonly", width=16,
            values=["thin", "thick", "eagerZeroedThick", "seSparse"]))
        grid_row(10, "vmx", "Dossier de sortie :", entry(self.imp_out_var), "la VM (.vmx + .vmdk) y sera créée")
        browse = ttk.Button(tgt, text="Parcourir…", command=self._browse_imp_out)
        browse.grid(row=10, column=3, sticky="w", **pad)
        self._imp_widgets["vmx"].append(browse)
        ssl = ttk.Checkbutton(tgt, text="Ignorer le certificat SSL (--noSSLVerify)", variable=self.no_ssl_var)
        ssl.grid(row=11, column=1, sticky="w", **pad)
        self._imp_widgets["esxi"].append(ssl)

        self.imp_props = ttk.LabelFrame(page, text="Propriétés OVF (une par ligne : cle=valeur, --prop)")
        self.imp_props.pack(fill="x", **pad)
        self.props_text = tk.Text(self.imp_props, height=3)
        self.props_text.pack(fill="x", padx=6, pady=6)

        self.imp_opt = ttk.Frame(page)
        self.imp_opt.pack(fill="x", **pad)
        self.imp_power_cb = ttk.Checkbutton(
            self.imp_opt, text="Allumer la VM après déploiement (--powerOn)",
            variable=self.imp_power_on_var)
        self.imp_power_cb.pack(side="left")
        self.imp_overwrite_cb = ttk.Checkbutton(
            self.imp_opt, text="Écraser si existant (--overwrite)",
            variable=self.imp_overwrite_var)
        self.imp_overwrite_cb.pack(side="left", padx=12)

        self.imp_target_var.trace_add("write", lambda *_: self._update_import_target())
        self._update_import_target()

    def _update_import_target(self):
        """Affiche uniquement les champs utiles à la cible choisie."""
        esxi = self.imp_target_var.get() == "esxi"
        for w in self._imp_widgets["esxi"]:
            w.grid() if esxi else w.grid_remove()
        for w in self._imp_widgets["vmx"]:
            w.grid_remove() if esxi else w.grid()
        if esxi:
            self.imp_props.pack(fill="x", padx=8, pady=4, before=self.imp_opt)
            self.imp_power_cb.pack(side="left", before=self.imp_overwrite_cb)
        else:
            self.imp_props.pack_forget()
            self.imp_power_cb.pack_forget()

    def _make_ds_combo(self, parent):
        self.ds_combo = ttk.Combobox(parent, textvariable=self.imp_datastore_var)
        return self.ds_combo

    def _list_datastores(self):
        fields = self._esxi_fields()
        if not fields:
            return
        host, user, password = fields
        self.ds_btn.configure(state="disabled")
        self._log(f"Recherche des datastores sur {host}…")

        def work():
            try:
                names = list_datastores(host, user, password, self.no_ssl_var.get())
                self.events.put(("datastores", names))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("ds_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _format_changed(self):
        vmx = self.format_var.get() == "vmx"
        for w in (self.vmx_disk_label, self.vmx_disk_combo):
            w.grid() if vmx else w.grid_remove()

    def _export_source_changed(self, _event=None):
        esxi = self.tabs.index(self.tabs.select()) == 1
        # La conversion vers .vmx ne concerne que les VM d'un serveur ESXi.
        self.vmx_radio.configure(state="normal" if esxi else "disabled")
        if not esxi and self.format_var.get() == "vmx":
            self.format_var.set("ova")
        for w in self.exp_esxi_opts:
            w.grid() if esxi else w.grid_remove()

    def _mode_changed(self, _event=None):
        importing = self.modes.index(self.modes.select()) == 1
        self.start_btn.configure(text="Importer / Déployer" if importing else "Exporter")

    def _browse_pkg(self):
        path = filedialog.askopenfilename(
            title="Sélectionner un paquet OVA / OVF",
            filetypes=[("OVA / OVF", "*.ova *.ovf"), ("Tous", "*.*")])
        if path:
            self.src_pkg_var.set(os.path.normpath(path))

    def _browse_imp_out(self):
        path = filedialog.askdirectory(title="Dossier de sortie")
        if path:
            self.imp_out_var.set(os.path.normpath(path))

    def _analyse(self):
        """Affiche dans le journal le contenu du paquet (nom, disques, réseaux...)."""
        ovftool = self._check_ovftool()
        pkg = self.src_pkg_var.get().strip()
        if not ovftool:
            return
        if not os.path.isfile(pkg):
            messagebox.showerror(APP_TITLE, "Sélectionnez un fichier .ova ou .ovf valide.")
            return
        self.analyse_btn.configure(state="disabled")
        self._log(f"Analyse de {pkg}…")

        def work():
            try:
                run_ovftool([ovftool, pkg], lambda line: self.events.put(("log", line)))
            except OSError as exc:
                self.events.put(("log", f"Impossible de lancer ovftool : {exc}"))
            self.events.put(("analysed", None))

        threading.Thread(target=work, daemon=True).start()

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
                elif kind == "datastores":
                    self.ds_combo["values"] = value
                    if value and not self.imp_datastore_var.get():
                        self.imp_datastore_var.set(value[0])
                    self._log(f"{len(value)} datastore(s) trouvé(s) : {', '.join(value)}")
                    self.ds_btn.configure(state="normal")
                elif kind == "ds_error":
                    self._log(f"Erreur : {value}")
                    self.ds_btn.configure(state="normal")
                    messagebox.showerror(APP_TITLE, value)
                elif kind == "analysed":
                    self.analyse_btn.configure(state="normal")
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
            target = os.path.join(out_dir, name, name + (".vmx" if fmt == "vmx" else ".ovf"))

        args = [ovftool, f"--targetType={fmt.upper()}", f"--name={name}"]
        if fmt != "vmx":
            args.append(f"--shaAlgorithm={self.sha_var.get()}")
        if self.overwrite_var.get():
            args.append("--overwrite")
        if self.eulas_var.get():
            args.append("--acceptAllEulas")
        if fmt != "vmx" and self.compress_var.get() > 0:
            args.append(f"--compress={self.compress_var.get()}")
        if fmt == "vmx" and not self.vmx_disk_var.get().startswith("("):
            args.append(f"--diskMode={self.vmx_disk_var.get()}")
        if is_esxi:
            if self.no_ssl_var.get():
                args.append("--noSSLVerify")
            if self.power_off_var.get():
                args.append("--powerOffSource")
        # Cible VMX : ovftool reçoit le dossier parent et crée <nom>/<nom>.vmx.
        args += [source, out_dir + os.sep if fmt == "vmx" else target]
        password = self.pass_var.get() if is_esxi else ""
        return args, target, password

    def _build_import_command(self, ovftool):
        """Commande d'import/déploiement ; renvoie (args, cible, mot de passe) ou None."""
        pkg = self.src_pkg_var.get().strip()
        if not os.path.isfile(pkg):
            messagebox.showerror(APP_TITLE, "Sélectionnez un fichier .ova ou .ovf valide.")
            return None
        name = self.imp_name_var.get().strip()
        if name and re.search(r'[\\/:*?"<>|]', name):
            messagebox.showerror(APP_TITLE, "Le nom contient des caractères interdits.")
            return None

        args = [ovftool]
        if name:
            args.append(f"--name={name}")
        if self.imp_overwrite_var.get():
            args.append("--overwrite")
        if self.eulas_var.get():
            args.append("--acceptAllEulas")

        if self.imp_target_var.get() == "vmx":
            out_dir = self.imp_out_var.get().strip()
            if not out_dir:
                messagebox.showerror(APP_TITLE, "Choisissez le dossier de sortie.")
                return None
            args.append("--targetType=VMX")
            return args + [pkg, out_dir + os.sep], out_dir, ""

        fields = self._esxi_fields()
        if not fields:
            return None
        host, user, password = fields
        if self.no_ssl_var.get():
            args.append("--noSSLVerify")
        if self.imp_datastore_var.get().strip():
            args.append(f"--datastore={self.imp_datastore_var.get().strip()}")
        if self.imp_network_var.get().strip():
            args.append(f"--network={self.imp_network_var.get().strip()}")
        if self.imp_folder_var.get().strip():
            args.append(f"--vmFolder={self.imp_folder_var.get().strip()}")
        args.append(f"--diskMode={self.imp_disk_var.get()}")
        if self.imp_power_on_var.get():
            args.append("--powerOn")
        for line in self.props_text.get("1.0", "end").splitlines():
            line = line.strip()
            if line:
                if "=" not in line:
                    messagebox.showerror(APP_TITLE, f"Propriété invalide (cle=valeur attendu) : {line}")
                    return None
                args.append(f"--prop:{line}")
        path = self.imp_path_var.get().strip().strip("/")
        target = vi_locator(host, user, password, quote(path, safe="/"))
        return args + [pkg, target], f"vi://{host}/{path}", password

    def _start(self):
        ovftool = self._check_ovftool()
        if not ovftool:
            return
        importing = self.modes.index(self.modes.select()) == 1
        self.op_label = "Import" if importing else "Export"
        built = self._build_import_command(ovftool) if importing else self._build_command(ovftool)
        if not built:
            return
        args, target, password = built

        if importing:
            if self.imp_target_var.get() == "vmx":
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror(APP_TITLE, f"Impossible de créer le dossier : {exc}")
                    return
        else:
            if os.path.exists(target) and not self.overwrite_var.get():
                messagebox.showerror(APP_TITLE, f"{target} existe déjà. Cochez « Écraser » pour le remplacer.")
                return
            try:
                parent = self.out_dir_var.get().strip() if self.format_var.get() == "vmx"                     else os.path.dirname(target)
                os.makedirs(parent, exist_ok=True)
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
            self._log(f"{self.op_label} annulé (un résultat partiel peut subsister).")
        elif code == 0:
            self.progress["value"] = 100
            self.status_var.set("Terminé")
            self._log(f"{self.op_label} terminé : {target}")
            messagebox.showinfo(APP_TITLE, f"{self.op_label} terminé :\n{target}")
        else:
            self.status_var.set("Échec")
            self._log(f"Échec ({self.op_label.lower()}, code {code}).")
            messagebox.showerror(APP_TITLE, f"Échec ({self.op_label.lower()}, code {code}). Voir le journal.")


if __name__ == "__main__":
    App().mainloop()
