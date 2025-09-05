/**
 * IBBot 性能测试脚本
 * 用于测试接口调用性能和缓存效果
 */

// 默认测试配置
const defaultTestConfig = {
    // 测试次数
    iterations: 5,
    // 测试间隔 (ms)
    interval: 500,
    // 测试超时 (ms)
    timeout: 30000,
    // 测试项目
    tests: [
        {
            name: 'chart_data接口',
            endpoint: '/api/chart_data?symbol=AAPL&interval=1min',
            description: '测试图表数据接口的缓存效果'
        },
        {
            name: 'orders接口',
            endpoint: '/api/orders',
            description: '测试订单数据接口的缓存效果'
        }
    ]
};

/**
 * 运行性能测试
 * @param {Object} config 测试配置
 * @returns {Promise<Object>} 测试结果
 */
async function runPerformanceTest(config = defaultTestConfig) {
    console.log(`开始性能测试，配置: ${config.iterations}次迭代, ${config.interval}ms间隔`);
    
    const results = {};
    
    for (const test of config.tests) {
        console.log(`\n测试: ${test.name} - ${test.description}`);
        
        const testResults = [];
        
        for (let i = 0; i < config.iterations; i++) {
            console.log(`迭代 ${i+1}/${config.iterations}:`);
            
            try {
                const startTime = performance.now();
                const response = await fetchWithTimeout(test.endpoint, {}, config.timeout);
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                const endTime = performance.now();
                const callTime = endTime - startTime;
                
                // 获取响应大小
                const clone = await response.clone();
                const text = await clone.text();
                const responseSize = new Blob([text]).size;
                
                testResults.push({
                    iteration: i+1,
                    callTime,
                    responseSize,
                    dataPoints: Array.isArray(data) ? data.length : 1
                });
                
                console.log(`  耗时: ${callTime.toFixed(2)}ms, 响应大小: ${formatBytes(responseSize)}, 数据点: ${Array.isArray(data) ? data.length : 1}`);
            } catch (error) {
                console.error(`  错误: ${error.message}`);
                testResults.push({
                    iteration: i+1,
                    error: error.message
                });
            }
            
            // 最后一次迭代不需要延迟
            if (i < config.iterations - 1) {
                await delay(config.interval);
            }
        }
        
        // 分析测试结果
        const successfulTests = testResults.filter(r => !r.error);
        
        if (successfulTests.length > 0) {
            const avgTime = successfulTests.reduce((sum, r) => sum + r.callTime, 0) / successfulTests.length;
            const avgSize = successfulTests.reduce((sum, r) => sum + r.responseSize, 0) / successfulTests.length;
            
            console.log(`\n结果分析:`);
            console.log(`  成功率: ${(successfulTests.length / config.iterations * 100).toFixed(2)}%`);
            console.log(`  平均响应时间: ${avgTime.toFixed(2)}ms`);
            console.log(`  平均响应大小: ${formatBytes(avgSize)}`);
            
            // 分析缓存效果
            if (config.iterations > 1 && successfulTests.length > 1) {
                const firstCall = successfulTests[0].callTime;
                const remainingCalls = successfulTests.slice(1);
                const avgRemainingTime = remainingCalls.reduce((sum, r) => sum + r.callTime, 0) / remainingCalls.length;
                
                const improvement = ((firstCall - avgRemainingTime) / firstCall * 100);
                
                console.log(`  缓存效果分析:`);
                console.log(`    首次调用: ${firstCall.toFixed(2)}ms`);
                console.log(`    后续平均: ${avgRemainingTime.toFixed(2)}ms`);
                console.log(`    性能提升: ${improvement.toFixed(2)}%`);
                
                if (improvement > 50) {
                    console.log(`    缓存效果: 极佳`);
                } else if (improvement > 20) {
                    console.log(`    缓存效果: 良好`);
                } else if (improvement > 0) {
                    console.log(`    缓存效果: 一般`);
                } else {
                    console.log(`    缓存效果: 未检测到`);
                }
            }
            
            results[test.name] = {
                successRate: successfulTests.length / config.iterations,
                avgTime,
                avgSize,
                testResults
            };
            
            // 添加缓存分析结果
            if (config.iterations > 1 && successfulTests.length > 1) {
                const firstCall = successfulTests[0].callTime;
                const remainingCalls = successfulTests.slice(1);
                const avgRemainingTime = remainingCalls.reduce((sum, r) => sum + r.callTime, 0) / remainingCalls.length;
                
                results[test.name].cacheAnalysis = {
                    firstCallTime: firstCall,
                    avgRemainingTime,
                    improvement: ((firstCall - avgRemainingTime) / firstCall * 100)
                };
            }
        } else {
            console.log(`\n结果分析: 所有测试均失败`);
            results[test.name] = {
                successRate: 0,
                errors: testResults.map(r => r.error)
            };
        }
    }
    
    console.log(`\n性能测试完成!`);
    return results;
}

/**
 * 带超时的fetch函数
 * @param {string} url 请求URL
 * @param {Object} options 请求选项
 * @returns {Promise<Response>} 响应对象
 */
async function fetchWithTimeout(url, options = {}) {
    const { timeout = 8000 } = options;
    
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);
    
    try {
        const response = await fetch(url, {
            ...options,
            signal: controller.signal
        });
        clearTimeout(id);
        return response;
    } catch (error) {
        clearTimeout(id);
        throw error;
    }
}

/**
 * 格式化字节大小
 * @param {number} bytes 字节数
 * @returns {string} 格式化后的字符串
 */
function formatBytes(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

/**
 * 延迟函数
 * @param {number} ms 延迟毫秒数
 * @returns {Promise<void>}
 */
function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// 导出函数供HTML页面使用
window.runPerformanceTest = runPerformanceTest;
window.fetchWithTimeout = fetchWithTimeout;
window.formatBytes = formatBytes;
window.delay = delay;

/**
 * 运行完整的自动化测试
 * @param {Object} config - 测试配置
 * @param {Function} updateProgressCallback - 更新进度的回调函数
 * @param {Function} logCallback - 日志输出的回调函数
 * @returns {Promise<Object>} 测试结果汇总
 */
async function runFullTest(config, updateProgressCallback, logCallback) {
    const log = (message) => {
        console.log(message);
        if (logCallback) logCallback(message);
    };
    
    log('开始自动化性能测试...');
    
    const totalTests = config.tests.length;
    const totalIterations = totalTests * config.iterations;
    let completedIterations = 0;
    
    const allResults = {};
    
    for (let i = 0; i < config.tests.length; i++) {
        const test = config.tests[i];
        log(`\n[${i+1}/${totalTests}] 测试: ${test.name} - ${test.description}`);
        
        const testResults = [];
        
        for (let j = 0; j < config.iterations; j++) {
            log(`迭代 ${j+1}/${config.iterations}:`);
            
            try {
                const startTime = performance.now();
                const response = await fetchWithTimeout(test.endpoint, {}, config.timeout);
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                const endTime = performance.now();
                const callTime = endTime - startTime;
                
                // 获取响应大小
                const clone = await response.clone();
                const text = await clone.text();
                const responseSize = new Blob([text]).size;
                
                testResults.push({
                    iteration: j+1,
                    callTime,
                    responseSize,
                    dataPoints: Array.isArray(data) ? data.length : 1
                });
                
                log(`  耗时: ${callTime.toFixed(2)}ms, 响应大小: ${formatBytes(responseSize)}, 数据点: ${Array.isArray(data) ? data.length : 1}`);
            } catch (error) {
                log(`  错误: ${error.message}`);
                testResults.push({
                    iteration: j+1,
                    error: error.message
                });
            }
            
            // 更新进度
            completedIterations++;
            if (updateProgressCallback) {
                updateProgressCallback(completedIterations / totalIterations * 100);
            }
            
            // 最后一次迭代不需要延迟
            if (j < config.iterations - 1) {
                await delay(config.interval);
            }
        }
        
        // 分析测试结果
        const successfulTests = testResults.filter(r => !r.error);
        
        if (successfulTests.length > 0) {
            const avgTime = successfulTests.reduce((sum, r) => sum + r.callTime, 0) / successfulTests.length;
            const avgSize = successfulTests.reduce((sum, r) => sum + r.responseSize, 0) / successfulTests.length;
            
            log(`\n结果分析:`);
            log(`  成功率: ${(successfulTests.length / config.iterations * 100).toFixed(2)}%`);
            log(`  平均响应时间: ${avgTime.toFixed(2)}ms`);
            log(`  平均响应大小: ${formatBytes(avgSize)}`);
            
            // 分析缓存效果
            if (config.iterations > 1 && successfulTests.length > 1) {
                const firstCall = successfulTests[0].callTime;
                const remainingCalls = successfulTests.slice(1);
                const avgRemainingTime = remainingCalls.reduce((sum, r) => sum + r.callTime, 0) / remainingCalls.length;
                
                const improvement = ((firstCall - avgRemainingTime) / firstCall * 100);
                
                log(`  缓存效果分析:`);
                log(`    首次调用: ${firstCall.toFixed(2)}ms`);
                log(`    后续平均: ${avgRemainingTime.toFixed(2)}ms`);
                log(`    性能提升: ${improvement.toFixed(2)}%`);
                
                let cacheEffectText = '';
                if (improvement > 50) {
                    cacheEffectText = '极佳';
                } else if (improvement > 20) {
                    cacheEffectText = '良好';
                } else if (improvement > 0) {
                    cacheEffectText = '一般';
                } else {
                    cacheEffectText = '未检测到';
                }
                log(`    缓存效果: ${cacheEffectText}`);
            }
            
            allResults[test.name] = {
                successRate: successfulTests.length / config.iterations,
                avgTime,
                avgSize,
                testResults
            };
            
            // 添加缓存分析结果
            if (config.iterations > 1 && successfulTests.length > 1) {
                const firstCall = successfulTests[0].callTime;
                const remainingCalls = successfulTests.slice(1);
                const avgRemainingTime = remainingCalls.reduce((sum, r) => sum + r.callTime, 0) / remainingCalls.length;
                
                allResults[test.name].cacheAnalysis = {
                    firstCallTime: firstCall,
                    avgRemainingTime,
                    improvement: ((firstCall - avgRemainingTime) / firstCall * 100)
                };
            }
        } else {
            log(`\n结果分析: 所有测试均失败`);
            allResults[test.name] = {
                successRate: 0,
                errors: testResults.map(r => r.error)
            };
        }
    }
    
    log(`\n性能测试完成!`);
    return allResults;
}

// 导出自动化测试函数
window.runFullTest = runFullTest;