# Bazzite Portal

A desktop app for Bazzite updates, tools, system management and troubleshooting.
Currently under development.

Weblate automatically generates the POT template and manages translations:
[Bazzite Portal on Weblate](https://hosted.weblate.org/projects/bazzite-portal/).

## Screenshots

**KDE Plasma**

<img src=".github/screenshots/home-plasma.png" alt="Bazzite Portal home on KDE Plasma" width="900">

**GNOME**

<img src=".github/screenshots/home-gnome.png" alt="Bazzite Portal home on GNOME" width="900">

## YAML configuration

The app expects two files in /usr/share/bazzite-portal/:

- tools.yml — tools and categories. [Template](templates/tools.yml).
- announcements.yml — announcements. [Template](templates/announcements.yml).

## Rebasing

Image choices are maintained in [rebase_images.json](bazzite_portal/maintenance/rebase_images.json).
Other desktops and Unstable can be shown by enabling **Show unsupported rebase targets** in Options.

## Logs

Basic information, warnings and errors are logged at INFO level, with a 6 MiB
storage limit. Detailed tracing is off by default. Use **Open app logs** in Options
to find the logs; troubleshooting reports include them.

## License

Code: [GPL-3.0-only](LICENSE). Artwork credits: [NOTICE.md](NOTICE.md).
Third-party license texts: [LICENSES](LICENSES/).

## Translations

Tool and announcement content, operation logs and command output are not translated.

## Development

Bump the app version ONLY in pyproject.toml under [project].version. Packaging derives
the other version fields automatically. Pushing a higher version to main creates
a GitHub release after a successful RPM build.

The RPM keeps application code in /usr/share/bazzite-portal and runs it with the
system Python (3.11 or newer). Python libraries, GTK, libadwaita and SDL3 remain
system package dependencies; the app is not tied to a Python minor-version directory.

## Temporary trial installation

### Bazzite for up to  Fedora 44

```sh
curl -fLO https://github.com/Xarishark/bazzite-portal/releases/latest/download/bazzite-portal.rpm
curl -fL https://raw.githubusercontent.com/Xarishark/bazzite-portal/main/templates/announcements.yml -o announcements.yml
sudo rpm-ostree usroverlay
sudo rpm -Uvh ./bazzite-portal.rpm
sudo rpm -e bazzite-updater
sudo install -d -m 755 /usr/share/bazzite-portal
sudo install -m 644 /usr/share/yafti/yafti.yml /usr/share/bazzite-portal/tools.yml
sudo install -m 644 announcements.yml /usr/share/bazzite-portal/announcements.yml
sudo systemctl daemon-reload
systemctl --user daemon-reload
systemctl --user start bazzite-portal-agent.service
bazzite-portal
```

### Bazzite on Fedora 45 and after (Packages need to be signed)

```sh
curl -fLO https://github.com/Xarishark/bazzite-portal/releases/latest/download/bazzite-portal.rpm
curl -fL https://raw.githubusercontent.com/Xarishark/bazzite-portal/main/templates/announcements.yml -o announcements.yml
sudo rpm-ostree usroverlay
sudo rpm --define '_pkgverify_level digest' -Uvh ./bazzite-portal.rpm
sudo rpm -e bazzite-updater
sudo install -d -m 755 /usr/share/bazzite-portal
sudo install -m 644 /usr/share/yafti/yafti.yml /usr/share/bazzite-portal/tools.yml
sudo install -m 644 announcements.yml /usr/share/bazzite-portal/announcements.yml
sudo systemctl daemon-reload
systemctl --user daemon-reload
systemctl --user start bazzite-portal-agent.service
bazzite-portal
```
