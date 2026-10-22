"""执行循环的内部件：facts / turn_log / login_branch / page_ready / budget / actions。

`agent_loop._run_loop` 仍是唯一编排者；这里只放它调用的叶子函数。
任何模块都不得 import `agent_loop`，否则会把环带回来。
"""
