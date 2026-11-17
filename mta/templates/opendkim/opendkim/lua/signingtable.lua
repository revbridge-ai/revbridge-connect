-- signingtable.lua
-- Lua dataset for OpenDKIM's SigningTable
-- Gets the global "query" and returns the key name for the KeyTable:
--   e.g. "rbds1._domainkey.dkim.shop.example"

-- =========================================================
-- 1) Log helpers
-- =========================================================
local LOG_FILE = "/var/log/opendkim-lua.log"

local function log_error(msg)
    local ok, err = pcall(function()
        local file = io.open(LOG_FILE, "a")
        if file then
            local timestamp = os.date("%Y-%m-%d %H:%M:%S")
            file:write(string.format("[%s] [ERROR] %s\n", timestamp, msg))
            file:close()
        end
    end)
end

-- =========================================================
-- 2) Load luasql.mysql
-- =========================================================
local luasql_success, luasql_module = pcall(require, "luasql.mysql")
if not luasql_success then
    log_error(string.format("Failed to load luasql.mysql: %s", tostring(luasql_module)))
    -- IMPORTANT: NEVER return nil to OpenDKIM; return an empty string instead
    return ""
end

local luasql = luasql_module
local env = luasql.mysql()
if not env then
    log_error("Failed to create luasql.mysql environment")
    return ""
end

-- =========================================================
-- 3) Database settings
-- =========================================================
-- Filled in by mta/render.py from the container environment
local DB_HOST = "{{ mysql_host }}"
local DB_USER = "{{ mailbox_user }}"
local DB_PASS = "{{ mailbox_password }}"
local DB_NAME = os.getenv("MYSQL_DATABASE") or "revbridge_connect"
local DB_PORT = tonumber("{{ mysql_port | default(3306) }}")
local CLUSTER_ID = "default"

-- =========================================================
-- 4) SQL / domain helpers
-- =========================================================
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

-- extracts a simple root domain (the last 2 labels)
local function extract_root_domain(domain)
    local parts = {}
    for label in string.gmatch(domain, "([^.]+)") do
        table.insert(parts, label)
    end
    local n = #parts
    if n >= 2 then
        return parts[n-1] .. "." .. parts[n]
    else
        return domain
    end
end


-- =========================================================
-- 5) Internal function: MySQL lookup
-- =========================================================
local function lookup_internal(func, query)
    -- When only one argument (query) is passed, it lands in "func"
    if query == nil and func ~= nil and type(func) == "string" then
        query = func
        func = nil
    end

    -- Validate the query
    if not query or query == "" then
        return ""
    end

    -- the query can be "user@sub.shop.example" or "sub.shop.example"
    local email = nil
    local full_domain = nil

    local at_pos = string.find(query, "@")
    if at_pos then
        email = query
        full_domain = string.sub(query, at_pos + 1)
    else
        full_domain = query
    end

    if not full_domain or full_domain == "" then
        return ""
    end

    local root_domain = extract_root_domain(full_domain)

    -- Database connection (protected with pcall)
    local success_conn, result = pcall(function()
        return env:connect(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
    end)

    if not success_conn then
        log_error(string.format("pcall failed to connect: %s", tostring(result)))
        return ""
    end

    local conn = result
    if not conn then
        log_error("connection returned nil (check DB credentials/host)")
        return ""
    end

    local function close_conn()
        if conn then
            pcall(function()
                conn:close()
            end)
        end
    end

    -- Priority rules:
    -- 1) exact match on the email (from_email)
    -- 2) exact match on the domain (from_domain = full_domain, wildcard_subdomains = 0)
    -- 3) wildcard on the domain/subdomains (from_domain = full_domain OR full_domain ends with .from_domain, wildcard_subdomains = 1)
    -- 4) wildcard on the root domain (from_domain = root_domain, wildcard_subdomains = 1)
    local conditions = {}
    local order_by = {}

    -- 1) email
    if email then
        table.insert(conditions, string.format("from_email = %s", escape_sql(email)))
        table.insert(order_by, string.format("(from_email = %s) DESC", escape_sql(email)))
    end

    -- 2) exact domain, no wildcard
    table.insert(
        conditions,
        string.format(
            "((from_email IS NULL OR from_email = '') AND wildcard_subdomains = 0 AND from_domain = %s)",
            escape_sql(full_domain)
        )
    )
    table.insert(
        order_by,
        string.format(
            "((from_email IS NULL OR from_email = '') AND wildcard_subdomains = 0 AND from_domain = %s) DESC",
            escape_sql(full_domain)
        )
    )

    -- 3) wildcard for the domain and its subdomains
    table.insert(
        conditions,
        string.format(
            "((from_email IS NULL OR from_email = '') AND wildcard_subdomains = 1 AND " ..
            "(from_domain = %s OR %s LIKE CONCAT('%%%%.', from_domain)))",
            escape_sql(full_domain),
            escape_sql(full_domain)
        )
    )
    table.insert(
        order_by,
        string.format(
            "((from_email IS NULL OR from_email = '') AND wildcard_subdomains = 1 AND from_domain = %s) DESC",
            escape_sql(full_domain)
        )
    )

    -- 4) wildcard on the root domain
    if root_domain ~= full_domain then
        table.insert(
            conditions,
            string.format(
                "((from_email IS NULL OR from_email = '') AND wildcard_subdomains = 1 AND from_domain = %s)",
                escape_sql(root_domain)
            )
        )
        table.insert(
            order_by,
            string.format(
                "((from_email IS NULL OR from_email = '') AND wildcard_subdomains = 1 AND from_domain = %s) DESC",
                escape_sql(root_domain)
            )
        )
    end

    local sql = string.format(
        [[
        SELECT selector, dkim_domain
        FROM dkim_identities
        WHERE cluster_id = %s
          AND (%s)
        ORDER BY %s
        LIMIT 1;
    ]],
        escape_sql(CLUSTER_ID),
        table.concat(conditions, " OR "),
        table.concat(order_by, ", ")
    )

    -- Optional debug

    -- Run the query
    local success_exec, result_exec = pcall(function()
        return conn:execute(sql)
    end)
    if not success_exec then
        log_error(string.format("pcall failed to execute SQL: %s", tostring(result_exec)))
        close_conn()
        return ""
    end

    local cursor = result_exec
    if not cursor then
        log_error("SQL execution returned no cursor")
        close_conn()
        return ""
    end

    local success_fetch, row = pcall(function()
        return cursor:fetch({}, "a")
    end)
    if not success_fetch then
        log_error(string.format("pcall failed to fetch row: %s", tostring(row)))
        pcall(function()
            cursor:close()
        end)
        close_conn()
        return ""
    end

    if not row then
        pcall(function()
            cursor:close()
        end)
        close_conn()
        return ""
    end

    local selector = row.selector
    local dkim_domain = row.dkim_domain

    if not selector or selector == "" or not dkim_domain or dkim_domain == "" then
        log_error(
            string.format(
                "invalid data from database: selector=%s, dkim_domain=%s",
                tostring(selector),
                tostring(dkim_domain)
            )
        )
        pcall(function()
            cursor:close()
        end)
        close_conn()
        return ""
    end

    pcall(function()
        cursor:close()
    end)
    close_conn()

    local keyname = string.format("%s._domainkey.%s", selector, dkim_domain)
    return keyname
end

-- =========================================================
-- 6) DATASET ENTRY POINT FOR OPENDKIM
-- =========================================================
-- When used as:
--   SigningTable lua:/etc/opendkim/signingtable.lua
-- OpenDKIM:
--   - sets the global variable `query` to the value to look up
--   - runs this file
--   - uses the returned value as the SigningTable answer

-- Keep the global query (it may be nil at startup)
local q = _G.query

-- CASE 1: nil or empty query (e.g. OpenDKIM's load test)
if q == nil or q == "" then
    return ""
end

-- CASE 2: valid query -> call lookup_internal under pcall
local ok, result = pcall(function()
    return lookup_internal(nil, q)
end)

if not ok then
    log_error("lookup_internal failed: " .. tostring(result))
    return ""
end

-- Make sure we never return nil
if result == nil then
    result = ""
end

-- *** THIS return IS WHAT OPENDKIM USES AS THE SIGNINGTABLE VALUE ***
return result