# COPR invokes this file with -f from the configured SCM subdirectory.
repo := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)

.PHONY: srpm
srpm:
	@test -n "$(outdir)" || { echo 'COPR outdir is required' >&2; exit 1; }
	dnf -y install rpm-build python3 python3-pip git tar gzip desktop-file-utils gettext appstream systemd-rpm-macros
	@set -eu; \
	mkdir -p "$(outdir)"; \
	output=$$(cd "$(outdir)" && pwd); \
	cd "$(repo)"; \
	work=$$(mktemp -d); \
	trap 'rm -rf "$$work"' EXIT; \
	export SOURCE_DATE_EPOCH=$$(git show -s --format=%ct HEAD); \
	python3 packaging/prepare_sources.py --output "$$work" --revision HEAD; \
	rpmbuild -bs --define "_topdir $$work" --define "_srcrpmdir $$output" \
	    "$$work/SPECS/bazzite-portal.spec"
