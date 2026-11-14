# noVNC 1.7.0 (vendored)

- Source: https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz
- sha256 of that tarball: b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6
- Taken unchanged: `core/` (MPL 2.0), `vendor/pako/` (MIT, see `vendor/pako/LICENSE`),
  `LICENSE.txt` and `AUTHORS`. Nothing else (no app UI, no tests).

Why vendored instead of `/usr/share/novnc`: Ubuntu 24.04 ships noVNC 1.3.0, other nodes and
the Mac panel ship other versions or none, and the viewer page imports the RFB core as an ES
module by path. A pinned copy in the package means the page behaves the same on every node,
works without the apt package, and can be served by the controller and the panel later.

To update: download the new release tarball into an empty directory, check its sha256 against
the GitHub release, replace `core/` and `vendor/pako/` here, update this file, and run the node
viewer tests.
