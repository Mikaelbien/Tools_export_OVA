# Tools_export_OVA

Outil graphique (Python / tkinter) qui **exporte** une machine virtuelle VMware en **OVA** ou **OVF**, et **importe / déploie** un OVA ou OVF en VM, en s'appuyant sur [VMware OVF Tool](documentation/VMware%20Ovf%20Tool.pdf) (`ovftool`).

**Export** : deux sources sont prises en charge :

- **VMware Workstation / Player** : une VM locale, à partir de son fichier `.vmx` ;
- **Serveur ESXi ou vCenter** : une VM de l'inventaire, atteinte via `vi://`.

## Pourquoi cet outil

`ovftool` est un outil en ligne de commande puissant mais aux options nombreuses. Cet outil fournit une interface simple pour les exports courants : choix de la source, de la destination et du format, suivi de la progression et journal.

## Utilisation

### Option 1 : exécutable (Windows)

Récupérer le dossier `release/` (voir [Construire l'exécutable](#construire-lexécutable)) et lancer `ExportOVA.exe`. Le dossier `ovftool/` doit rester à côté de l'exe.

### Option 2 : depuis les sources

Prérequis : Python 3.9+ (tkinter inclus) et OVF Tool.

```
python export_ova.py
```

### Étapes dans l'interface

1. **OVF Tool** : le chemin est détecté automatiquement (voir [Détection d'ovftool](#détection-dovftool)). Sinon, cliquer sur *Parcourir…*.
2. **Choisir la source** via l'onglet :
   - *Workstation / Player (.vmx)* : sélectionner le fichier `.vmx` de la VM ;
   - *Serveur ESXi / vCenter* : saisir le serveur, l'utilisateur et le mot de passe, puis cliquer sur *Lister les VM* et choisir la VM. Le chemin peut aussi être saisi à la main, par exemple `MaVM` (ESXi seul) ou `Datacenter/vm/MaVM` (vCenter).
3. **Destination** : dossier de sortie, nom du fichier (par défaut le nom de la VM) et format :
   - **OVA** : un fichier unique `nom.ova` ;
   - **OVF** : un sous-dossier `nom/` contenant `nom.ovf`, le manifeste et les disques `.vmdk`.
4. **Options** (voir ci-dessous), puis cliquer sur **Exporter**.
5. Suivre la barre de progression et le journal ; **Annuler** interrompt `ovftool`.

### Convertir une VM ESXi en .vmx (Workstation)

Dans l'onglet *Exporter*, source *Serveur ESXi / vCenter*, choisir le format **VMX (Workstation, depuis ESXi)** : `ovftool` crée directement `dossier/nom/nom.vmx` et les disques `.vmdk`, ouvrables dans Workstation / Player. Le format des disques (`--diskMode`) est au choix : monolithicSparse, twoGbMaxExtentSparse, monolithicFlat, twoGbMaxExtentFlat. Ce format n'est pas proposé pour une source `.vmx` locale.

### Importer / Déployer

L'onglet *Importer / Déployer* transforme un `.ova` / `.ovf` en machine virtuelle :

1. Choisir le fichier ; *Analyser* affiche dans le journal le contenu du paquet (nom, disques, réseaux, EULA).
2. Choisir la cible :
   - **ESXi / vCenter** : serveur, utilisateur et mot de passe (communs avec l'onglet Exporter), puis nom de la VM (optionnel), datastore (`--datastore`), réseau (`--network`), dossier VM (`--vmFolder`), format des disques (`--diskMode` : thin, thick, eagerZeroedThick, seSparse). Sur un ESXi seul, le chemin cible reste vide ; sur vCenter, indiquer par exemple `Datacenter/host/Cluster`.
   - **Workstation / Player** : dossier de sortie ; `ovftool` y crée la VM (`.vmx` + `.vmdk`) avec `--targetType=VMX`.
3. *Propriétés OVF* : une ligne `cle=valeur` par propriété, transmise avec `--prop:cle=valeur` (adresse IP, mot de passe, etc., selon le paquet).
4. *Allumer la VM après déploiement* ajoute `--powerOn`.

### Options (export)

| Option | Effet (option `ovftool`) |
|---|---|
| Écraser si existant | `--overwrite` |
| Accepter les EULA | `--acceptAllEulas` |
| Ignorer le certificat SSL (ESXi) | `--noSSLVerify` : utile avec le certificat auto-signé d'un ESXi |
| Éteindre la VM source (ESXi) | `--powerOffSource` |
| Compression 1-9 (0 = aucune) | `--compress=N` : 9 = plus lent, plus petit |
| Empreinte du manifeste | `--shaAlgorithm=sha1\|sha256\|sha512` |

La commande exacte exécutée est affichée dans le journal, le mot de passe étant masqué.

## Remarques

- La VM doit être **éteinte** pour être exportée (une VM suspendue ne convient pas). Pour ESXi, l'option *Éteindre la VM source* s'en charge.
- Le listage des VM ESXi interroge l'inventaire via `ovftool` ; en cas d'échec, saisir le chemin de la VM manuellement.
- Les identifiants sont passés à `ovftool` dans l'URL `vi://` (caractères spéciaux encodés en `%XX`) ; ils ne sont pas enregistrés.
- Prévoir sur le disque de destination au moins la taille des disques de la VM.

## Détection d'ovftool

L'outil cherche `ovftool` dans cet ordre :

1. `ovftool/ovftool.exe` à côté du script ou de l'exécutable (copie portable, voir [ovftool/README.md](ovftool/README.md)) ;
2. le `PATH` ;
3. les emplacements d'installation habituels (`C:\Program Files\VMware\VMware OVF Tool`, dossier OVFTool de Workstation).

Les binaires d'`ovftool` (~95 Mo) ne sont pas versionnés dans git ; voir `.gitignore`.

## Construire l'exécutable

```
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name ExportOVA --distpath release export_ova.py
```

Puis copier le contenu du dossier d'installation d'OVF Tool dans `release/ovftool/`. Le dossier `release/` est ignoré par git.

## Structure du dépôt

```
export_ova.py        application (interface + appel à ovftool)
ovftool/             copie portable d'OVF Tool (binaires non versionnés)
documentation/       documentation officielle d'OVF Tool (PDF)
release/             exécutable + ovftool, généré (non versionné)
```
