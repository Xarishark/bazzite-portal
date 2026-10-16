# Bazzite Portal

A desktop app for Bazzite updates, tools, system management and troubleshooting.
Currently under development.

> Translation templates (POT) are regenerated automatically by GitHub Actions when
> source changes reach main. Contributors do not need to regenerate them manually;
> translations are maintainehttps://hosted.weblate.org/translate/bazzite-portald in [Weblate](https://weblate.org/).

## Screenshots

**KDE Plasma**

<img src=".github/screenshots/home-plasma.png" alt="Bazzite Portal home on KDE Plasma" width="900">

**GNOME**

<img src=".github/screenshots/home-gnome.png" alt="Bazzite Portal home on GNOME" width="900">

## Preview

From the repository folder, run:

```sh
python3 -m bazzite_portal --preview templates/tools.yml
```

Preview mode does not run tool commands or change the system.

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

This project is translated using [Weblate](https://weblate.org/).
Tool and announcement content, operation logs and command output are not translated.

## Development

Bump the app version ONLY in pyproject.toml under [project].version. Packaging derives
the other version fields automatically. Pushing a higher version to main creates
a GitHub release after a successful RPM build.

## Temporary trial installation

```sh
curl -fL https://github.com/Xarishark/bazzite-portal/releases/latest/download/bazzite-portal.rpm -o bazzite-portal.rpm
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
