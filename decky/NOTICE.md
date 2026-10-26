# Third-party notices

The monochrome plugin icon derives its symbol paths from Portal's original
`bazzite_portal/assets/indicator/bazzite-logo.svg`, from the Bazzite project
(https://github.com/ublue-os/bazzite), under Apache-2.0. The colored background is
omitted and the existing symbol paths inherit Decky's foreground color. The
original artwork remains unchanged. See `licenses/Apache-2.0.txt` in the ZIP.

The bundled Jeepney client uses the MIT license in `licenses/JEEPNEY_LICENSE`.
Notices for JavaScript included in the bundle are in `THIRD_PARTY_LICENSES.txt`.

The release-note reader in `src/release-notes/ReleaseNotes.tsx` and `styles.ts`
is adapted from Bazzite Buddy, branch `fix/release-notes`, revision
`0dd0db84288b1de959393f6470fe93cfada8c0c0`:
https://github.com/xXJSONDeruloXx/bazzite-buddy

The controller scrolling, X Refresh / B Back behavior, scoped readable Markdown
and table styles are retained. Steam internal release-news patches are omitted.
Identity comes exclusively from Portal worker snapshots; no Buddy system queries
are included. The bounded GitHub lookup mirrors Portal's existing matching rules.

The upstream BSD-3-Clause notice follows unchanged:

BSD 3-Clause License

Copyright (c) 2024, Hypothetical Plugin Developer
Original Copyright (c) 2022-2024, Steam Deck Homebrew

All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
