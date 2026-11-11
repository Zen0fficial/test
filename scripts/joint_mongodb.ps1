<#
.SYNOPSIS
Reject the retired project-local MongoDB installer and service controls.
.DESCRIPTION
Joint task workflows use SQLite coordination and immutable Parquet history.
This compatibility stub performs no download, filesystem mutation or process
management. Use the commands documented in docs/qlib-parquet-tasks.md instead.
#>
param([ValidateSet('Install', 'Start', 'Stop', 'Status')][string]$Action = 'Status')

$ErrorActionPreference = 'Stop'
throw 'MongoDB task support is retired. Use the local Parquet task workflow documented in docs/qlib-parquet-tasks.md.'
