【再次强调】整条回复只能是一个 JSON 对象。有校验点 JSON 时必须返回 checkpoint_results，每个原 point_id 恰好一项，并包含布尔 passed 和该项具体 evidence；不要返回 verify_points，不要只返回总体判断。输入值的范围与截断证据仍须满足上述规则。
