# Tools_export_OVA

Interface graphique (Python / tkinter, sans dépendance) pour exporter une VM en **OVA** ou **OVF** avec VMware OVF Tool :

- VM VMware Workstation / Player (fichier `.vmx`) ;
- VM d'un serveur ESXi / vCenter (`vi://`), avec listage des VM.

## Prérequis

- Python 3.9+ (tkinter inclus)
- [VMware OVF Tool](documentation/VMware%20Ovf%20Tool.pdf) installé (détecté automatiquement, sinon à indiquer dans l'interface)

## Lancement

```
python export_ova.py
```

Les VM doivent être éteintes (ou cocher « Éteindre la VM source » pour ESXi).
