# .github

general github settings

* [Managing your profile README - GitHub Docs](https://docs.github.com/en/account-and-profile/how-tos/profile-customization/managing-your-profile-readme)
* [Customizing your organization&#39;s profile - GitHub Docs](https://docs.github.com/en/organizations/collaborating-with-groups-in-organizations/customizing-your-organizations-profile)

## Shared GitHub Actions

* [`actions/sdlc-stage`](actions/sdlc-stage/README.md) and the reusable workflows in [`.github/workflows`](.github/workflows): the SDLC pipeline (Ollama designs, Claude Code or a person builds, deterministic checks verify, Ollama reviews).
* [`actions/security-scan`](actions/security-scan/README.md): gitleaks and Trivy, failing only on what a branch adds; step 5 of the SDLC pipeline.
* [`actions/commit-delta-summary`](actions/commit-delta-summary/README.md): an Ollama summary of a commit range, posted on the pull request.
