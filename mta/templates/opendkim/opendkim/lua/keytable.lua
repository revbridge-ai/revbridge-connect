-- keytable.lua
-- Lua dataset for OpenDKIM's KeyTable
-- When used as:
--   KeyTable lua:/etc/opendkim/keytable.lua
--
-- OpenDKIM:
--   - sets the global variable `query` to the key name
--   - runs this script
--   - expects the script to RETURN 3 VALUES:
--       (a) dkim_domain (d=)
--       (b) selector   (s=)
--       (c) private_key (inline key material, read from dkim_keys.private_key_pem)

-- =========================================================
-- 1) Log helper
-- =========================================================
local LOG_FILE = "/var/log/opendkim-lua.log"

local function log_error(msg)
    local ok, err = pcall(function()
        local file = io.open(LOG_FILE, "a")
        if file then
            local timestamp = os.date("%Y-%m-%d %H:%M:%S")
            file:write(string.format("[%s] [KEYTABLE] [ERROR] %s\n", timestamp, msg))
            file:close()
        end
    end)
end

-- =========================================================
-- 2) Load luasql.mysql
-- =========================================================
local luasql_ok, luasql_module = pcall(require, "luasql.mysql")
if not luasql_ok then
    log_error("Failed to load luasql.mysql: " .. tostring(luasql_module))
    return "", "", ""
end

local luasql = luasql_module
local env = luasql.mysql()
if not env then
    log_error("Failed to create luasql.mysql environment")
    return "", "", ""
end

-- =========================================================
-- 3) Database settings
-- =========================================================
local DB_HOST = "{{ mysql_host }}"
local DB_USER = "{{ mailbox_user }}"
local DB_PASS = "{{ mailbox_password }}"
local DB_NAME = os.getenv("MYSQL_DATABASE") or "revbridge_connect"
local DB_PORT = tonumber("{{ mysql_port | default(3306) }}")
local CLUSTER_ID = "default"

local function escape_sql(str)
    if not str then
        return "NULL"
    end
    -- MySQL treats the backslash as an escape character, so it must be escaped
    -- too (doubling only the quote lets  \'  break out of the literal). Control
    -- characters are dropped.
    local clean = string.gsub(str, "[%z\1-\31]", "")
    clean = string.gsub(clean, "\\", "\\\\")
    clean = string.gsub(clean, "'", "''")
    return "'" .. clean .. "'"
end

-- =========================================================
-- 4) Internal function: MySQL lookup
-- =========================================================
local function lookup_internal(query)
    if not query or query == "" then
        return nil, nil, nil
    end

    -- expected query: "selector._domainkey.dkim_domain"
    local selector, dkim_domain = string.match(query, "^([^.]+)%.%_domainkey%.(.+)$")
    if not selector or not dkim_domain then
        log_error(string.format("Failed to parse query '%s' - expected format: selector._domainkey.dkim_domain", tostring(query)))
        return nil, nil, nil
    end

    local ok_conn, conn_or_err = pcall(function()
        return env:connect(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
    end)

    if not ok_conn then
        log_error("pcall failed to connect: " .. tostring(conn_or_err))
        return nil, nil, nil
    end

    local conn = conn_or_err
    if not conn then
        log_error("connection returned nil (check DB credentials/host)")
        return nil, nil, nil
    end

    local function close_conn()
        if conn then
            pcall(function() conn:close() end)
        end
    end

    local sql = string.format([[
        SELECT private_key_pem
        FROM dkim_keys
        WHERE cluster_id = %s
          AND selector    = %s
          AND dkim_domain = %s
        LIMIT 1;
    ]],
        escape_sql(CLUSTER_ID),
        escape_sql(selector),
        escape_sql(dkim_domain)
    )

    local ok_exec, cursor_or_err = pcall(function()
        return conn:execute(sql)
    end)

    if not ok_exec then
        log_error("pcall failed to execute SQL: " .. tostring(cursor_or_err))
        close_conn()
        return nil, nil, nil
    end

    local cursor = cursor_or_err
    if not cursor then
        log_error("SQL execution returned no cursor")
        close_conn()
        return nil, nil, nil
    end

    local ok_fetch, row_or_err = pcall(function()
        return cursor:fetch({}, "a")
    end)

    if not ok_fetch then
        log_error("pcall failed to fetch row: " .. tostring(row_or_err))
        pcall(function() cursor:close() end)
        close_conn()
        return nil, nil, nil
    end

    local row = row_or_err
    if not row then
        pcall(function() cursor:close() end)
        close_conn()
        return nil, nil, nil
    end

    local private_key_pem = row.private_key_pem

    pcall(function() cursor:close() end)
    close_conn()

    if not private_key_pem or private_key_pem == "" then
        log_error(string.format(
            "empty private_key_pem for %s._domainkey.%s: the key was never stored in the database",
            tostring(selector), tostring(dkim_domain)))
        return nil, nil, nil
    end

    -- Returns the key MATERIAL, not a path: OpenDKIM accepts either as the 3rd
    -- value. With no file on disk, the domain signs as soon as its key is stored
    -- in the database (the lookup runs per message).
    return dkim_domain, selector, private_key_pem
end

-- =========================================================
-- 5) Dataset entry point
-- =========================================================
local q = _G.query

-- During init OpenDKIM may call it with query=nil
if q == nil or q == "" then
    return "", "", ""
end

local ok, dkim_domain, selector, private_key_pem = pcall(function()
    return lookup_internal(q)
end)

if not ok then
    log_error("lookup_internal failed: " .. tostring(dkim_domain))
    return "", "", ""
end

-- lookup_internal found nothing
if not dkim_domain or dkim_domain == "" or
   not selector or selector == "" or
   not private_key_pem or private_key_pem == "" then
    return "", "", ""
end

-- *** THIS return is what OpenDKIM uses ***
-- 3 values: domain, selector, key MATERIAL (not a path)
return dkim_domain, selector, private_key_pem
