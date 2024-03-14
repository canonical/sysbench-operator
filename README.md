Connect your database to sysbench and run a benchmark.

# sysbench-perf-operator

Connect your database to sysbench and run a benchmark.

## Getting started

To start your testing, run the following steps:
1) relate sysbench-operator to the target database
2) prepare the database with: `juju run <sysbench-app> prepare`
3) execute it with `juju run <sysbench-app> run`

## Monitoring

The sysbench-perf-operator supports COS integration.

For that, it is enough to relate this charm to `grafana-agent`.
The charm will open a scrape endpoint for Prometheus to collect
metrics.

# Supported Databases

Currently supports: MySQL

# TODOs

* Develop a grafana dashboard
