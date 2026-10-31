// Read-only single-stock earnings report page lookup, FTQuote build hash-gated.
// Protocol20846 FinancialDataProxyReq.BatchFetchStockFRUrlReq, normal channel1.
var reportPending = null;
var reportLastRequestAt = 0;
function reportFinish(pending, result) {
    if (reportPending !== pending) return;
    reportPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol:20846, channel:1, request_id:pending.requestId,
        request:pending.metadata, readonly:true}, result));
}
var reportReplyObserver = Interceptor.attach(historyCoreModule.base.add(0xa4f80), {
    onEnter(args) {
        const pending=reportPending;
        if (!pending || pending.requestId===null) return;
        try {
            if (uint64(args[1].toString()).toString()!==pending.requestId || args[2].toInt32()!==1) return;
            const response=args[4];
            if(response.isNull()) throw Error('Report response is null');
            if(response.add(8).readU16()!==20846) return;
            const size=Number(response.add(0x18).readU64().toString());
            if(!Number.isSafeInteger(size)||size<0||size>65536) throw Error('Report response exceeds bound');
            const body=response.add(0x10).readPointer();
            if(size&&body.isNull()) throw Error('Report body is null');
            reportFinish(pending,{status:'response_received',transport_error:args[3].toInt32(),
                payload_bytes:size?Array.from(new Uint8Array(body.readByteArray(size))):[]});
        } catch(error) {reportFinish(pending,{status:'response_error',message:String(error),payload_bytes:[]});}
    }
});
api.requestreportlink = function(stockId, period) {
    if(typeof stockId!=='string'||!/^\d+$/.test(stockId)||!Number.isSafeInteger(Number(stockId))||Number(stockId)<=0)
        throw Error('Invalid stock ID');
    if(snapshotUnknown(uint64(stockId))) throw Error('Stock is unknown to the native catalog');
    if(typeof period!=='string'||!/^(19[7-9]\d|20\d\d)Q[1-4]$/.test(period)) throw Error('Expected fiscal period YYYYQ1..YYYYQ4');
    if(reportPending) throw Error('A report request is already pending');
    if(Date.now()-reportLastRequestAt<500) throw Error('Report requests are limited to two per second');
    const item=historyField(1,Number(stockId)).concat(snapshotMessage(2,Array.from(period).map(c=>c.charCodeAt(0))));
    const bytes=snapshotMessage(1,snapshotMessage(1,item));
    return new Promise(function(resolve){
        const body=Memory.alloc(bytes.length+1);body.writeByteArray(bytes);
        const data=Memory.alloc(40);data.writeByteArray(new Uint8Array(40));data.writeU16(20846);
        const string=data.add(8);
        if(bytes.length<16)string.writeByteArray(bytes);else string.writePointer(body);
        string.add(16).writeU64(bytes.length);string.add(24).writeU64(Math.max(15,bytes.length));
        const callback=Memory.alloc(64);callback.writeByteArray(new Uint8Array(64));
        const pending={resolve:resolve,requestId:null,metadata:{stock_id:stockId,fiscal_period:period},refs:[body,data,callback],timer:null};
        reportPending=pending;
        pending.timer=setTimeout(()=>reportFinish(pending,{status:'response_timeout',payload_bytes:[]}),8000);
        try{
            reportLastRequestAt=Date.now();
            pending.requestId=historySendCommand(1,data,ptr(0),callback,ptr(0)).toString();
            if(pending.requestId==='0')reportFinish(pending,{status:'request_rejected',payload_bytes:[]});
        }catch(error){reportFinish(pending,{status:'request_error',message:String(error),payload_bytes:[]});}
    });
};
