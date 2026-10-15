# OCI Scripts

**English (en-US)** · [Português (pt-BR)](README.pt-BR.md)

A collection of scripts for automating everyday tasks in Oracle Cloud Infrastructure (OCI).

Usage instructions, requirements, and examples are available in the documentation within each directory.

## Disclaimer

These scripts are provided as is, without warranties of availability, security, or suitability for a particular environment. No support, maintenance, updates, or service-level commitments are provided.

Scripts run with the permissions of the configured OCI account and may create, modify, or delete resources and incur charges. Before running a script, read its documentation and review the target tenancy, region, compartments, permissions, and affected resources. Validate its behavior in a test environment and maintain appropriate backups.

Users are responsible for their use and modification of this material. To the extent permitted by applicable law, authors and contributors accept no liability for data loss, charges, unavailability, or other damage resulting from its use. References to Oracle and OCI do not imply endorsement or certification by Oracle.

## Available scripts

| Script | Purpose | Repository |
| --- | --- | --- |
| import-custom-images | Import QCOW2 or VMDK disks from URLs or local files as custom images, individually or in JSON batches. Includes concurrent transfers, conversion monitoring, recovery, logs, and cleanup. | [import-custom-images](import-custom-images/) |

## Getting started

Clone the repository:

```bash
git clone https://github.com/wuilber002/oci-scripts.git
cd oci-scripts
```

Choose a script from the catalog and enter its directory, replacing `SCRIPT_DIRECTORY` with the corresponding directory name:

```bash
cd SCRIPT_DIRECTORY
```

Read the README in that directory for requirements, permissions, parameters, and execution examples.
