ALTER SYSTEM ALTER CONFIGURATION ('global.ini', 'system') UNSET ('resource_tracking', 'enable_tracking') with reconfigure;
ALTER SYSTEM ALTER CONFIGURATION ('global.ini', 'system') UNSET ('resource_tracking', 'memory_tracking') with reconfigure;
ALTER SYSTEM ALTER CONFIGURATION ('global.ini', 'system') UNSET ('memorymanager', 'statement_memory_limit') with reconfigure;
