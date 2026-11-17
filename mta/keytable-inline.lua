-- keytable-inline.lua — KeyTable dataset that returns the DKIM private key MATERIAL inline
-- (dkim_keys.private_key_pem) instead of a file path. OpenDKIM accepts the 3rd return value as
-- "private_key OR file path" (see the stock keytable.lua header), so this removes the on-disk
-- key materialization ENTIRELY: a new domain signs the INSTANT its key is committed to
-- the DB — OpenDKIM does a live lookup per message. Zero window, no key sync, no key files.
--
-- DB creds come from env (the opendkim process inherits the container env): MYSQL_HOST,
-- MAILBOX_USER, MAILBOX_PASSWORD. (To encrypt keys at rest, decrypt the column here.)

local LOG_FILE = "/var/log/opendkim-lua.log"
local function log_error(msg)
    pcall(function()
        local f = io.open(LOG_FILE, "a")
        if f then f:write(string.format("[%s] [KEYTABLE-INLINE] [ERROR] %s\n", os.date("%Y-%m-%d %H:%M:%S"), msg)); f:close() end
    end)
end

local luasql_ok, luasql = pcall(require, "luasql.mysql")
if not luasql_ok then log_error("load luasql.mysql failed: " .. tostring(luasql)); return "", "", "" end
local env = luasql.mysql()
if not env then log_error("luasql.mysql() nil"); return "", "", "" end

local DB_HOST = os.getenv("MYSQL_HOST") or "127.0.0.1"
local DB_USER = os.getenv("MAILBOX_USER") or ""
local DB_PASS = os.getenv("MAILBOX_PASSWORD") or ""
local DB_NAME = os.getenv("MYSQL_DATABASE") or "revbridge_connect"
local DB_PORT = tonumber(os.getenv("MYSQL_PORT") or "3306")
local CLUSTER_ID = "default"

local function esc(s)
  if not s then return "NULL" end
  -- backslash is an escape character in MySQL: escape it too, drop control chars
  local clean = string.gsub(s, "[%z\1-\31]", "")
  clean = string.gsub(clean, "\\", "\\\\")
  clean = string.gsub(clean, "'", "''")
  return "'" .. clean .. "'"
end

local function lookup(query)
    if not query or query == "" then return nil, nil, nil end
    local selector, dkim_domain = string.match(query, "^([^.]+)%.%_domainkey%.(.+)$")
    if not selector or not dkim_domain then
        log_error("bad query: " .. tostring(query)); return nil, nil, nil
    end
    local ok_c, conn = pcall(function() return env:connect(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT) end)
    if not ok_c or not conn then log_error("connect failed: " .. tostring(conn)); return nil, nil, nil end
    local function close() pcall(function() conn:close() end) end
    local sql = string.format(
        "SELECT private_key_pem FROM dkim_keys WHERE cluster_id=%s AND selector=%s AND dkim_domain=%s LIMIT 1;",
        esc(CLUSTER_ID), esc(selector), esc(dkim_domain))
    local ok_e, cur = pcall(function() return conn:execute(sql) end)
    if not ok_e or not cur then log_error("execute failed: " .. tostring(cur)); close(); return nil, nil, nil end
    local row = nil
    pcall(function() row = cur:fetch({}, "a") end)
    pcall(function() cur:close() end); close()
    if not row or not row.private_key_pem or row.private_key_pem == "" then
        log_error("no private_key_pem for " .. dkim_domain); return nil, nil, nil
    end
    -- *** return the KEY MATERIAL inline (not a path) ***
    return dkim_domain, selector, row.private_key_pem
end

local q = _G.query
if q == nil or q == "" then return "", "", "" end
local ok, d, s, k = pcall(function() return lookup(q) end)
if not ok or not d or d == "" or not s or s == "" or not k or k == "" then return "", "", "" end
return d, s, k
