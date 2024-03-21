# sysbench-operator

Connect your database to sysbench and run a benchmark.

## Getting started

To start your testing, run the following steps:
1) relate sysbench-operator to the target database
2) prepare the database with: `juju run <sysbench-unit> prepare`
3) execute it with `juju run <sysbench-unit> run`

Optionally, set the size or duration of the test with:
```
juju config tables=N threads=Y duration=D
```

To stop the sysbench, either wait for its duration to finish
or run the action:
```
juju run <sysbench-unit> stop
```

Finally, once the test is finished, it is possible to clean up
the data pushed to the database with:
```
juju run <sysbench-unit> clean
```

## Monitoring

The sysbench-operator supports COS integration.

For that, it is enough to relate this charm to `grafana-agent`.
The charm will open a scrape endpoint for the agent to collect
metrics.

There is currently no supported Grafana dashboards. Data can
be accessed directly on Prometheus.

# Supported Databases

Currently supports: MySQL, Postgresql
