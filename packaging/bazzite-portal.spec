# Generate with python3 packaging/metadata.py; build build/packaging/bazzite-portal.spec.
%global appid io.github.Xarishark.bazzite_portal
%global _empty_manifest_terminate_build 0
Name:           bazzite-portal
Version:        @VERSION@
Release:        1
Summary:        Bazzite tools and reconnectable system updates
URL:            https://github.com/Xarishark/bazzite-portal
License:        GPL-3.0-only AND LGPL-3.0-only AND LGPL-3.0-or-later AND Apache-2.0 AND CC0-1.0 AND ISC AND MIT AND BSD-3-Clause
Source0:        %{name}-%{version}.tar.gz
Source1:        %{name}-decky-%{version}.zip
BuildArch:      noarch
BuildRequires:  python3 >= 3.11
BuildRequires:  desktop-file-utils
BuildRequires:  gettext
BuildRequires:  appstream
BuildRequires:  systemd-rpm-macros
Requires:       python3 >= 3.11
Requires:       python3-gobject >= 3.42
Requires:       python3-pyyaml >= 6
Requires:       gobject-introspection
Requires:       gtk4 >= 4.10
Requires:       libadwaita >= 1.4
Requires:       SDL3 >= 3.2

%description
A GTK desktop application for Bazzite Portal tools, pinned actions, system
updates, rollback and system information.

%prep
%autosetup

%build
python3 packaging/translations.py build
python3 packaging/prepare_sources.py --verify-zip %{SOURCE1} --source-info packaging/source.json

%install
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -a bazzite_portal %{buildroot}%{_datadir}/%{name}/
find %{buildroot}%{_datadir}/%{name} -type d -name __pycache__ -prune -exec rm -rf {} +
find %{buildroot}%{_datadir}/%{name} -type f -name '*.py[co]' -delete
find %{buildroot}%{_datadir}/%{name} -type d -exec chmod 755 {} +
find %{buildroot}%{_datadir}/%{name} -type f -exec chmod 644 {} +
install -m 644 pyproject.toml %{buildroot}%{_datadir}/%{name}/pyproject.toml
install -Dm644 %{SOURCE1} %{buildroot}%{_datadir}/%{name}/decky/bazzite-portal.zip
install -Dm755 packaging/bazzite-portal %{buildroot}%{_bindir}/bazzite-portal
install -Dm755 packaging/bazzite-portal-maintenance %{buildroot}%{_libexecdir}/bazzite-portal-maintenance
install -Dm644 build/packaging/%{appid}.desktop %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -Dm644 build/packaging/%{appid}.metainfo.xml %{buildroot}%{_datadir}/metainfo/%{appid}.metainfo.xml
install -Dm644 bazzite_portal/assets/indicator/bazzite-portal.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
install -Dm644 packaging/%{appid}.service %{buildroot}%{_datadir}/dbus-1/services/%{appid}.service
install -Dm644 build/packaging/bazzite-portal-agent.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/bazzite-portal-agent.desktop
install -Dm644 packaging/bazzite-portal-agent.service %{buildroot}%{_userunitdir}/bazzite-portal-agent.service
install -Dm644 'packaging/bazzite-portal-maintenance@.service' '%{buildroot}%{_unitdir}/bazzite-portal-maintenance@.service'
install -Dm644 packaging/50-bazzite-portal.rules %{buildroot}%{_datadir}/polkit-1/rules.d/50-bazzite-portal.rules
install -Dm755 packaging/bazzite-portal-uupd-flatpak %{buildroot}%{_libexecdir}/bazzite-portal-uupd-flatpak
install -Dm755 packaging/bazzite-portal-uupd-brew %{buildroot}%{_libexecdir}/bazzite-portal-uupd-brew

mkdir -p %{buildroot}%{_datadir}/locale
cp -a build/locale/. %{buildroot}%{_datadir}/locale/
if [ -n "$(find build/locale -name '*.mo' -print -quit)" ]; then
    %find_lang %{name}
else
    touch %{name}.lang
fi

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
appstreamcli validate --no-net %{buildroot}%{_datadir}/metainfo/%{appid}.metainfo.xml

%post
systemctl daemon-reload >/dev/null 2>&1 || :
%systemd_user_post bazzite-portal-agent.service

%postun
systemctl daemon-reload >/dev/null 2>&1 || :
%systemd_user_postun bazzite-portal-agent.service

%files -f %{name}.lang
%license LICENSE LICENSES NOTICE.md
%doc README.md
%{_bindir}/bazzite-portal
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/bazzite_portal/
%{_datadir}/%{name}/pyproject.toml
%{_datadir}/%{name}/decky/
%{_libexecdir}/bazzite-portal-maintenance
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/metainfo/%{appid}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_datadir}/dbus-1/services/%{appid}.service
%config(noreplace) %{_sysconfdir}/xdg/autostart/bazzite-portal-agent.desktop
%{_userunitdir}/bazzite-portal-agent.service
%{_unitdir}/bazzite-portal-maintenance@.service
%{_datadir}/polkit-1/rules.d/50-bazzite-portal.rules
%{_libexecdir}/bazzite-portal-uupd-flatpak
%{_libexecdir}/bazzite-portal-uupd-brew
