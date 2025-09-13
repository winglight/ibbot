// 测试风险规则订单过滤逻辑
const testOrders = [
    { order_id: '372', symbol: 'MNQ', order_source: 'tp', rule_id: 'MNQ', status: 'Submitted' },
    { order_id: '102', symbol: 'MNQ', order_source: 'tp', rule_id: 'MNQ', status: 'Submitted' },
    { order_id: '62', symbol: 'MNQ', order_source: 'tp', rule_id: 'MNQ', status: 'Filled' },
    { order_id: '999', symbol: 'AAPL', order_source: 'manual', rule_id: null, status: 'Filled' },
    { order_id: '888', symbol: 'MNQ', order_source: 'sl', rule_id: 'MNQ', status: 'Submitted' }
];

function testRiskRuleFilter(symbol) {
    const target = String(symbol || '').toUpperCase();
    console.log('测试目标symbol:', target);
    
    const riskRuleOrders = testOrders.filter(order => {
        const os = String(order.order_source || '').toLowerCase();
        const isRisk = os === 'sl' || os === 'tp' || String(order.trigger_type || '').toLowerCase() === 'risk_rule';
        const symU = String(order.symbol || '').toUpperCase();
        const ridU = String(order.rule_id || '').toUpperCase();
        const matches = isRisk && (symU === target || ridU === target);
        
        console.log(`订单${order.order_id}: os=${os}, isRisk=${isRisk}, symU=${symU}, ridU=${ridU}, matches=${matches}`);
        
        return matches;
    });
    
    console.log('过滤结果:', riskRuleOrders.map(o => o.order_id));
    return riskRuleOrders;
}

// 测试MNQ
console.log('=== 测试MNQ ===');
testRiskRuleFilter('MNQ');

console.log('\n=== 测试mnq (小写) ===');
testRiskRuleFilter('mnq');