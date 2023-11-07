Connect your database to sysbench and run a benchmark.

# sysbench-perf-operator

Connect your database to sysbench and run a benchmark.

## Getting started

To start your testing, run the following steps:
1) relate sysbench-perf-operator to the target database
2) upload a test script (TPCC format)
3) Execute it with `juju run <sysbench-app> sysbench-run`

### Uploading test scripts

Sysbench has a set of tests that are native, but its most
interesting feature is its extensibility using LUA scripts.

Upload your favorite scripts as `juju resources` to the
application. For each unit, run:

```
juju attach-resource sysbench-perf-operator/0 script=<path to zip file>
```

The charm expects a ZIP file containing the script.

## Monitoring

The sysbench-perf-operator supports COS integration.

For that, it is enough to relate this charm to `grafana-agent`.
The charm will open a scrape endpoint for Prometheus to collect
metrics.

# Supported Databases

Currently supports: MySQL

# TODOs

* Develop a grafana dashboard
