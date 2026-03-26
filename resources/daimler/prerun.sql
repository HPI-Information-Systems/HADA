ALTER SYSTEM ALTER CONFIGURATION ('global.ini', 'system') SET ('resource_tracking', 'enable_tracking') = 'on' with reconfigure;
ALTER SYSTEM ALTER CONFIGURATION ('global.ini', 'system') SET ('resource_tracking', 'memory_tracking') = 'on' with reconfigure;
ALTER SYSTEM ALTER CONFIGURATION ('global.ini', 'system') SET ('memorymanager', 'statement_memory_limit') = '100' with reconfigure;
