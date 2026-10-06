# OXP Norm Testing

This folder contains a local Docker stack and interactive test script for OXP norm testing.

## Requirements

- Project: `mas-lab-dev`
  - Repo: https://github.com/pablo4dev/mas-lab-dev
  - Branch: `feat/oxp-norm-testing`
- Sibling project: `oxp-lib`
  - Branch: `main`

Expected layout (sibling repositories):

```text
<parent>/
  mas-lab-dev/
  oxp-lib/
```

## Run

Launch two terminals and `cd` into the `mas-lab-dev` project root.

### Terminal 1

```bash
cd tests/oxp-norm-testing

docker compose -f compose_oxp_norm_testing.yml --profile with-noa up --build
docker compose -f compose_oxp_norm_testing.yml --profile with-noa up
```

### Terminal 2

```bash
cd tests/oxp-norm-testing/scripts
./test.sh
```
