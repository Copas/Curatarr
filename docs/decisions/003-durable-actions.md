# 003: Persist external actions

The webhook only writes an event. The worker turns observations into persistent, uniquely keyed actions and executes them after commit. Failed acquisition may retry. An uncertain deletion response enters an unknown state and requires reconciliation rather than automatic retry.
