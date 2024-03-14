#!/usr/bin/env sysbench

-- Copyright 2018 Percona, LLC

-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at

--     http://www.apache.org/licenses/LICENSE-2.0

-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.


-- This program is free software; you can redistribute it and/or modify
-- it under the terms of the GNU General Public License as published by
-- the Free Software Foundation; either version 2 of the License, or
-- (at your option) any later version.

-- This program is distributed in the hope that it will be useful,
-- but WITHOUT ANY WARRANTY; without even the implied warranty of
-- MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
-- GNU General Public License for more details.

-- You should have received a copy of the GNU General Public License
-- along with this program; if not, write to the Free Software
-- Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

-- ----------------------------------------------------------------------
-- TPCC-like workload
-- ----------------------------------------------------------------------

require("tpcc_common")
require("tpcc_run")
require("tpcc_check")

function thread_init()
   drv,con=db_connection_init()
end

function event()
  -- print( NURand (1023,1,3000))
  local max_trx =  sysbench.opt.enable_purge == "yes" and 24 or 23
  local trx_type = sysbench.rand.uniform(1,max_trx)
  if trx_type <= 10 then
    trx="new_order"
  elseif trx_type <= 20 then
    trx="payment"
  elseif trx_type <= 21 then
    trx="orderstatus"
  elseif trx_type <= 22 then
    trx="delivery"
  elseif trx_type <= 23 then
    trx="stocklevel"
  elseif trx_type <= 24 then
    trx="purge"
  end

-- Execute transaction
   _G[trx]()

end

function sysbench.hooks.before_restart_event(err)
  con:query("ROLLBACK")
end

function sysbench.hooks.report_intermediate(stat)
-- --   print("my stat: ", val)
   if  sysbench.opt.report_csv == "yes" then
   	sysbench.report_csv(stat)
   else
   	sysbench.report_default(stat)
   end
end


-- vim:ts=4 ss=4 sw=4 expandtab
