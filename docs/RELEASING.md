# Releasing

How a version gets from this repository to somebody's Unraid box.

## The short version

```bash
git tag v1.0.0 && git push origin v1.0.0
```

`.github/workflows/release.yml` runs the whole suite, then builds
`linux/amd64` and `linux/arm64` and pushes to `ghcr.io/datbird/couchelephant`
tagged `1.0.0`, `1.0`, `1` and `latest`. An image that fails its own tests is
never published: the release workflow calls the CI workflow and waits for it.

Version numbers follow semver. `VERSION` in `app/routes/_shared.py` is what the
About page shows, so move it in the same commit you tag.

## Where the image lives

**GitHub Container Registry**, `ghcr.io/datbird/couchelephant`. Public images
there cost nothing, allow anonymous pulls, and have no published pull limit.
Docker Hub allows 100 anonymous pulls per IP address per six hours, which is a
poor fit for an app strangers install: one busy neighbour on the same shared
address can lock everyone else out.

Docker Hub carries the same image, at `datbird/couchelephant`. It is where
people browse and search, which GHCR has no page for. The release workflow
pushes to both, driven by two secrets and one variable on the repository:

| Where | Name | Value |
| --- | --- | --- |
| Repository variable | `DOCKERHUB_IMAGE` | `datbird/couchelephant` |
| Repository secret | `DOCKERHUB_USERNAME` | the Docker Hub account |
| Repository secret | `DOCKERHUB_TOKEN` | a Docker Hub access token, write scope |

Remove `DOCKERHUB_IMAGE` and the Docker Hub steps skip; the release still
succeeds and GHCR still gets the image. The Unraid template points at ghcr.io
either way, for the pull-limit reason above.

Docker Hub's description and long description are not updated by the push. Set
them from the README with a PATCH to
`https://hub.docker.com/v2/repositories/datbird/couchelephant/`.

### If the GHCR package ever comes back private

Published from a public repository, the package inherits that and is public
from its first push. It only needs setting by hand if it was first published
while the repository was private:

1. github.com/datbird → **Packages** → **couchelephant**
2. **Package settings** → **Danger Zone** → **Change visibility** → **Public**

This is one-way. A public package cannot be made private again.

The `org.opencontainers.image.source` label in the `Dockerfile` links the
package to this repository, which is what puts it on the repository sidebar.

## Unraid Community Applications

The template is **not** in this repository. CA reads one dedicated public repo
per maintainer: [datbird/unraid-templates](https://github.com/datbird/unraid-templates).

Submission goes through <https://ca.unraid.net/submit>, signed in with the
unraid.net or Unraid forum account. The portal scans the repository with the
same pipeline the CA build uses and lists what it finds. There is no pull
request to file and no forum post required.

What the scan insists on:

- The template repository is public, not archived, and carries an OSI licence
  at its root. This covers the templates only; the app's own licence is
  separate, and the app's source does not have to be public.
- `ca_profile.xml` sits in the root with a non-empty `<Profile>`.
- Every `<Repository>` names an image that can actually be pulled.
- None of the starter template's placeholder text or artwork survives.

A Docker-only submission with no duplicate is usually approved automatically
and appears at the next feed build, which happens about every four hours.
Anything needing a human takes a few business days.

Changing a template afterwards needs no resubmission. CA re-reads each file
from its own `TemplateURL` on every build, so a push is the whole procedure.

## Order of operations for the first release

1. Make this repository public. The template's `Project`, `Support` and
   `ReadMe` links point here, and a listing whose links 404 is worse than no
   listing.
2. Tag `v1.0.0` and let the release workflow publish the image.
3. Make the GHCR package public, as above.
4. Make `datbird/unraid-templates` public.
5. Submit that repository at <https://ca.unraid.net/submit>.

Steps 3 and 4 cannot be undone, so they come after the image is proven.
