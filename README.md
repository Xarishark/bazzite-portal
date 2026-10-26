# Bazzite Portal

[![Translation status](https://hosted.weblate.org/widget/bazzite-portal/app/svg-badge.svg?capitalize=1)](https://hosted.weblate.org/engage/bazzite-portal/)

A desktop app for Bazzite updates, tools, system management and troubleshooting.
Currently under development.

Weblate automatically generates the POT template and manages translations:
[Bazzite Portal on Weblate](https://hosted.weblate.org/projects/bazzite-portal/).

## Screenshots

**KDE Plasma**

<img src=".github/screenshots/home-plasma.png" alt="Bazzite Portal home on KDE Plasma" width="900">

**GNOME**

<img src=".github/screenshots/home-gnome.png" alt="Bazzite Portal home on GNOME" width="900">

## Icon themes

| Spinning | Bouncing | Spinning Mono | Bouncing Mono | Native (Plasma) | Native (GNOME) |
| :---: | :---: | :---: | :---: | :---: | :---: |
| <img src=".github/screenshots/icon-themes/spinning.gif" alt="Spinning icon animation" width="96"> | <img src=".github/screenshots/icon-themes/bouncing.gif" alt="Bouncing icon animation" width="96"> | <img src=".github/screenshots/icon-themes/spinning-mono.gif" alt="Monochrome spinning icon animation" width="96"> | <img src=".github/screenshots/icon-themes/bouncing-mono.gif" alt="Monochrome bouncing icon animation" width="96"> | <img src=".github/screenshots/icon-themes/native-plasma.gif" alt="Native Plasma icon animation" width="96"> | <img src=".github/screenshots/icon-themes/native-gnome.gif" alt="Native GNOME bouncing arrow animation" width="96"> |

## CLI Flags

| Command | Opens |
| --- | --- |
| `bazzite-portal` | Home |
| `bazzite-portal --updates` | Updates, including when Portal is already open |
| `bazzite-portal --action-id TOOL_ID` | The tool matching an `id` in `tools.yml` |

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

Bump the desktop app version in pyproject.toml under [project].version. Bump the
Decky plugin version separately in decky/package.json only when releasing plugin
changes. Packaging derives the other version fields automatically. Pushing a
higher app version to main creates a GitHub release after a successful RPM build.

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
