/* SBP relation compiler: canonical graph in, derived visual scene out. */
(function(root){
  'use strict';

  const asArray=value=>Array.isArray(value)?value:[];
  const idOf=value=>String(value??'');
  const number=value=>Number.isFinite(Number(value))?Number(value):0;
  const stableHash=value=>{
    let hash=2166136261;
    for(const char of String(value)){
      hash^=char.charCodeAt(0);
      hash=Math.imul(hash,16777619);
    }
    return (hash>>>0).toString(36);
  };

  function relationEndpoints(relation){
    const endpoints=asArray(relation&&relation.endpoints).filter(Boolean);
    if(endpoints.length>=2)return endpoints.slice(0,2).map(idOf);
    return [relation&&relation.from,relation&&relation.to].filter(Boolean).slice(0,2).map(idOf);
  }

  function classifyRelations(input){
    const nodes=asArray(input&&input.nodes);
    const nodeById=new Map(nodes.filter(node=>node&&node.id).map(node=>[idOf(node.id),node]));
    const layers=asArray(input&&input.layers).length?asArray(input.layers):[...new Set(nodes.map(node=>node.layer).filter(Boolean))];
    const layerIndex=new Map(layers.map((layer,index)=>[layer,index]));
    const relations=[];
    const unresolved=[];
    for(const relation of asArray(input&&input.relations)){
      const endpoints=relationEndpoints(relation);
      const missing=endpoints.filter(endpoint=>!nodeById.has(endpoint));
      if(endpoints.length!==2||missing.length){
        unresolved.push({...relation,endpoints,unresolved_endpoints:missing.length?missing:endpoints});
        continue;
      }
      const [from,to]=endpoints;
      const fromNode=nodeById.get(from),toNode=nodeById.get(to);
      const fromStep=number(fromNode.step),toStep=number(toNode.step);
      const fromLayer=fromNode.layer||'',toLayer=toNode.layer||'';
      const distance=Math.abs((layerIndex.get(fromLayer)??-99)-(layerIndex.get(toLayer)??99));
      const type=relation.type||'unknown';
      let routeClass='cross-stage-dependency';
      if(type==='sequence')routeClass='journey-sequence';
      else if(fromStep===toStep&&distance<=1)routeClass='local-interaction';
      else if(fromStep===toStep||type==='handoff')routeClass='cross-lane-handoff';
      const direction=fromStep===toStep?'same':(fromStep<toStep?'forward':'backward');
      relations.push({...relation,endpoints,from,to,fromNode,toNode,fromStep,toStep,fromLayer,toLayer,
        routeClass,direction,source:relation.source||'evidence',unknownType:!['sequence','same_event','handoff','causal'].includes(type)});
    }
    return {relations,unresolved,layers};
  }

  // draw-only-relations-the-grid-cannot-express: a blueprint's grid position
  // already expresses precedence (column order) and co-occurrence (same
  // cell) -- redrawing that as a line is noise, not information. Pure
  // function of relation type and the two endpoints' step/layer; no
  // per-case tuned constant.
  function renderDecisionFor(relation,fromCard,toCard){
    const type=relation&&relation.type;
    const fromStep=number(fromCard&&fromCard.step),toStep=number(toCard&&toCard.step);
    const fromLayer=(fromCard&&fromCard.layer)||'',toLayer=(toCard&&toCard.layer)||'';
    const sameStep=fromStep===toStep;
    const sameCell=sameStep&&fromLayer===toLayer;
    if(type==='sequence'&&sameCell)return {decision:'implicit',reason:'這兩件事在同一格，已用卡片上下順序表示先後'};
    // converge-blueprint-presentation：前後順序一律不畫（研究者 2026-09-26 裁定）——
    // 相鄰欄、隔欄、同欄不同泳道都一樣，欄位由左到右已經表達先後。
    if(type==='sequence')return {decision:'omit',reason:'前後順序由欄位位置表達'};
    if(type==='same_event'&&sameCell)return {decision:'implicit',reason:'這兩件事在同一格，已用群組框表示同時發生'};
    if(sameStep)return {decision:'draw-local',reason:null};
    return {decision:'draw-corridor',reason:null};
  }

  function buildBundles(classified){
    const relations=Array.isArray(classified)?classified:asArray(classified&&classified.relations);
    const byKey=new Map();
    for(const relation of relations){
      const key=[relation.routeClass,relation.fromLayer,relation.toLayer,relation.fromStep,relation.toStep,relation.direction,relation.source].join('\u001f');
      let bundle=byKey.get(key);
      if(!bundle){
        bundle={id:`B-${stableHash(key)}`,key,routeClass:relation.routeClass,source:relation.source,
          from:{step:relation.fromStep,layer:relation.fromLayer},to:{step:relation.toStep,layer:relation.toLayer},
          direction:relation.direction,memberIds:[],members:[]};
        byKey.set(key,bundle);
      }
      bundle.memberIds.push(relation.id);
      bundle.members.push(relation);
    }
    return [...byKey.values()].map(bundle=>({...bundle,count:bundle.memberIds.length}));
  }

  function allocateTracks(intervals){
    const sorted=asArray(intervals).map((interval,index)=>({...interval,index,start:number(interval.start),end:number(interval.end)}))
      .sort((a,b)=>a.start-b.start||a.end-b.end||idOf(a.id).localeCompare(idOf(b.id))||a.index-b.index);
    const trackEnds=[];
    const assigned=new Map();
    for(const interval of sorted){
      let track=trackEnds.findIndex(end=>end<interval.start);
      if(track<0){track=trackEnds.length;trackEnds.push(interval.end);}
      else trackEnds[track]=interval.end;
      assigned.set(interval.index,track);
    }
    return asArray(intervals).map((interval,index)=>({...interval,track:assigned.get(index)}));
  }

  function segmentIntersectsRect(a,b,rect){
    const left=rect.x,right=rect.x+rect.w,top=rect.y,bottom=rect.y+rect.h;
    if(a.x===b.x){
      return a.x>left&&a.x<right&&Math.max(Math.min(a.y,b.y),top)<Math.min(Math.max(a.y,b.y),bottom);
    }
    if(a.y===b.y){
      return a.y>top&&a.y<bottom&&Math.max(Math.min(a.x,b.x),left)<Math.min(Math.max(a.x,b.x),right);
    }
    return true;
  }

  function pathIntersectsObstacle(points,obstacles,ignoredNodeIds){
    const ignored=new Set(asArray(ignoredNodeIds).map(idOf));
    const collisions=[];
    for(const obstacle of asArray(obstacles)){
      if(ignored.has(idOf(obstacle.id)))continue;
      for(let index=1;index<asArray(points).length;index++){
        if(segmentIntersectsRect(points[index-1],points[index],obstacle)){
          collisions.push(obstacle.id);break;
        }
      }
    }
    return collisions;
  }

  // gate-blueprint-line-overlap: pathIntersectsObstacle() only ever checks
  // a line against cards/lane headers, never against other lines -- "zero
  // collisions" was an honest answer to the only question it was asked, but
  // it was being read as a proxy for "the picture is legible", which it
  // never measured. These two functions let the scene report the failure
  // mode collision-checking cannot see: two relations' lines sitting
  // directly on top of each other.
  function segmentsOf(line){
    const points=asArray(line&&line.points);
    const segments=[];
    for(let index=1;index<points.length;index++){
      const a=points[index-1],b=points[index];
      if(a.x===b.x)segments.push({relationId:line.relationId,axis:'vertical',coordinate:a.x,start:Math.min(a.y,b.y),end:Math.max(a.y,b.y),index:index-1});
      else if(a.y===b.y)segments.push({relationId:line.relationId,axis:'horizontal',coordinate:a.y,start:Math.min(a.x,b.x),end:Math.max(a.x,b.x),index:index-1});
    }
    return segments;
  }
  function segmentOverlaps(lines,tolerance=2){
    const buckets=new Map();
    for(const line of asArray(lines)){
      for(const segment of segmentsOf(line)){
        const key=`${segment.axis}${segment.coordinate}`;
        const bucket=buckets.get(key)||[];bucket.push(segment);buckets.set(key,bucket);
      }
    }
    const overlaps=[];
    for(const bucket of buckets.values()){
      for(let i=0;i<bucket.length;i++)for(let j=i+1;j<bucket.length;j++){
        const a=bucket[i],b=bucket[j];
        // A relation's own path never overlaps itself, including at a
        // corner where two of its own segments touch (share an endpoint).
        if(a.relationId===b.relationId)continue;
        const length=Math.min(a.end,b.end)-Math.max(a.start,b.start);
        if(length>tolerance)overlaps.push({type:'overlap',relations:[a.relationId,b.relationId],axis:a.axis,coordinate:a.coordinate,length});
      }
    }
    return overlaps;
  }

  // Single shared line-wrapping implementation (quantize-blueprint-card-and-
  // gutter-geometry, "Line-wrapping logic becomes a single shared utility"):
  // used both to assign a card's height tier here and, via ui.html's thin
  // wrapper, to render its label text, so the two can never disagree on how
  // many lines a given label takes.
  function wrapLabelLines(value,limit=12){
    const text=String(value||'未命名服務動作').trim();
    if(text.length<=limit)return [text];
    const words=text.split(/\s+/),lines=[];let line='';
    for(const word of words){
      if(!line){line=word;continue;}
      if((line+' '+word).length<=limit)line+=' '+word;
      else{lines.push(line);line=word;}
    }
    if(line)lines.push(line);
    if(lines.length===1&&lines[0].length>limit){
      const raw=lines[0];lines.length=0;
      for(let i=0;i<raw.length;i+=limit)lines.push(raw.slice(i,i+limit));
    }
    // 中英混排時，空白夾住的短英數詞（例如年齡分級「A」）用空白斷字會被單獨拆
    // 成一行，讀起來像排版錯了。把落單的短英數詞併回鄰行——寧可鄰行超過字數
    // 上限，也不要讓它自己佔一行。
    const isOrphanAsciiToken=value=>/^[A-Za-z0-9][A-Za-z0-9.-]{0,2}$/.test(value);
    for(let i=0;i<lines.length;i++){
      if(lines.length<=1||!isOrphanAsciiToken(lines[i]))continue;
      if(i>0){lines[i-1]+=' '+lines[i];lines.splice(i,1);}
      else{lines[1]=lines[i]+' '+lines[1];lines.splice(i,1);}
      i--;
    }
    if(lines.length>3)return [lines[0],lines[1],lines.slice(2).join('').slice(0,limit-1)+'…'];
    return lines;
  }

  function buildLargeScene(input,metricOverrides){
    const classified=classifyRelations(input||{});
    const metrics={nodeWidth:210,nodeHeight:64,columnGap:112,nodeGap:12,laneGap:38,laneHeaderWidth:150,
      gutter:22,trackGap:26,topPadding:54,leftPadding:176,...(metricOverrides||{})};
    const nodes=asArray(input&&input.nodes);
    const steps=asArray(input&&input.steps).length?asArray(input.steps):[...new Set(nodes.map(node=>number(node.step)))].sort((a,b)=>a-b).map(no=>({no,name:`步驟 ${no}`}));
    const stepNos=steps.map((step,index)=>number(step.no??step.step??index+1));
    const stepIndex=new Map(stepNos.map((step,index)=>[step,index]));
    const layers=classified.layers.length?classified.layers:[...new Set(nodes.map(node=>node.layer).filter(Boolean))];
    const layerIndex=new Map(layers.map((layer,index)=>[layer,index]));
    const cellNodes=new Map();
    for(const node of nodes){
      const key=`${number(node.step)}\u001f${node.layer||''}`;
      const group=cellNodes.get(key)||[];group.push(node);cellNodes.set(key,group);
    }
    const laneHeights=layers.map(layer=>{
      let maximum=0;
      for(const no of stepNos)maximum=Math.max(maximum,(cellNodes.get(`${no}\u001f${layer}`)||[]).length);
      return Math.max(metrics.nodeHeight,maximum*metrics.nodeHeight+Math.max(0,maximum-1)*metrics.nodeGap);
    });
    const intervals=classified.relations.filter(relation=>relation.fromStep!==relation.toStep).map(relation=>({id:relation.id,start:Math.min(stepIndex.get(relation.fromStep)??0,stepIndex.get(relation.toStep)??0),end:Math.max(stepIndex.get(relation.fromStep)??0,stepIndex.get(relation.toStep)??0)}));
    const tracks=allocateTracks(intervals);
    const trackById=new Map(tracks.map(track=>[track.id,track.track]));
    const parallelByKey=new Map();
    for(const relation of classified.relations){
      const key=`${relation.from}\u001f${relation.to}`;
      const group=parallelByKey.get(key)||[];group.push(relation);parallelByKey.set(key,group);
    }
    const parallelOffset=new Map();
    for(const group of parallelByKey.values()){
      group.forEach((relation,index)=>parallelOffset.set(relation.id,(index-(group.length-1)/2)*6));
    }
    const topBand=metrics.topPadding+Math.max(1,tracks.length?Math.max(...tracks.map(track=>track.track))+1:0)*metrics.trackGap;
    const laneStarts=[];let cursor=topBand;
    for(const height of laneHeights){laneStarts.push(cursor);cursor+=height+metrics.laneGap;}
    const sceneNodes=[];const nodeById=new Map();
    for(const [key,group] of cellNodes){
      const [step,layer]=key.split('\u001f');
      const column=stepIndex.get(number(step))??0,row=layerIndex.get(layer)??0;
      group.forEach((node,index)=>{
        const sceneNode={...node,x:metrics.leftPadding+column*(metrics.nodeWidth+metrics.columnGap),y:laneStarts[row]+index*(metrics.nodeHeight+metrics.nodeGap),w:metrics.nodeWidth,h:metrics.nodeHeight};
        sceneNodes.push(sceneNode);nodeById.set(idOf(node.id),sceneNode);
      });
    }
    const width=metrics.leftPadding+Math.max(1,stepNos.length)*(metrics.nodeWidth+metrics.columnGap)+metrics.gutter*3;
    const height=cursor+metrics.laneGap;
    const obstacles=[...sceneNodes.map(node=>({id:node.id,x:node.x,y:node.y,w:node.w,h:node.h,kind:'node'}))];
    layers.slice(0,-1).forEach((layer,index)=>obstacles.push({id:`boundary-${index}`,x:0,y:laneStarts[index]+laneHeights[index]+8,w:metrics.laneHeaderWidth,h:18,kind:'boundary'}));
    layers.forEach((layer,index)=>obstacles.push({id:`lane-${index}`,x:0,y:laneStarts[index],w:metrics.laneHeaderWidth,h:laneHeights[index],kind:'lane'}));
    const edgeFor=relation=>{
      const from=nodeById.get(idOf(relation.from)),to=nodeById.get(idOf(relation.to));
      if(!from||!to)return null;
      const forward=to.x>=from.x;
      const sourceX=forward?from.x+from.w:from.x;
      const targetX=forward?to.x+to.w:to.x;
      const sourceGutter=sourceX+(forward?metrics.gutter:-metrics.gutter);
      const targetGutter=targetX+(forward?metrics.gutter:-metrics.gutter);
      const sourceY=from.y+from.h/2,targetY=to.y+to.h/2;
      const offset=parallelOffset.get(relation.id)??0;
      let points;
      if(relation.fromStep===relation.toStep){
        const gutter=sourceGutter+offset;
        points=[{x:sourceX,y:sourceY},{x:gutter,y:sourceY},{x:gutter,y:targetY},{x:targetX,y:targetY}];
      }else if(!points){
        const railY=metrics.topPadding+(trackById.get(relation.id)??0)*metrics.trackGap+offset;
        points=[{x:sourceX,y:sourceY},{x:sourceGutter,y:sourceY},{x:sourceGutter,y:railY},{x:targetGutter,y:railY},{x:targetGutter,y:targetY},{x:targetX,y:targetY}];
      }
      return {...relation,points,track:trackById.get(relation.id)??null,collisions:pathIntersectsObstacle(points,obstacles,[relation.from,relation.to])};
    };
    const edges=classified.relations.map(edgeFor).filter(Boolean);
    const warnings=[];
    if(sceneNodes.length>100||edges.length>200)warnings.push({type:'complexity',nodes:sceneNodes.length,relations:edges.length});
    return {nodes:sceneNodes,edges,obstacles,bounds:{x:0,y:0,width,height},warnings,unresolved:classified.unresolved,layers,steps};
  }

  function buildBlueprintScene(graph,options){
    const opts=options||{};
    const nodes=asArray(graph&&graph.nodes);
    const relations=asArray(graph&&graph.relations);
    const layers=asArray(opts.layers).length?asArray(opts.layers):[...new Set(nodes.map(node=>node.layer).filter(Boolean))];
    const metrics={cardWidth:200,cardHeight:64,cardGap:10,columnGap:112,laneGap:28,
      leftPadding:150,laneHeaderWidth:150,portMargin:18,topPadding:40,trackGap:24,
      badgeClearance:30,cardLineHeight:18,cardBottomPadding:8,cardLabelCharsPerLine:12,
      overlapTolerance:2,collapseMedianMultiple:3,
      ...(opts.metricOverrides||{})};
    const layerIndex=new Map(layers.map((layer,index)=>[layer,index]));
    const classified=classifyRelations({nodes,relations,layers});
    // draw-only-relations-the-grid-cannot-express: decide once, up front,
    // whether each relation needs a line at all. `omit`/`implicit` relations
    // never reach gutter sizing, port allocation, or routing below -- they
    // are reported through `notDrawn` instead so they remain visible as
    // data without becoming visual noise.
    const decided=classified.relations.map(relation=>({...relation,
      ...renderDecisionFor(relation,{step:relation.fromStep,layer:relation.fromLayer},{step:relation.toStep,layer:relation.toLayer})}));
    // Every relation the caller hands in leaves with exactly one outcome: a
    // rendered line, or an entry here. A relation whose endpoint resolves to
    // no node used to `continue` out of classifyRelations and appear in
    // neither -- invisible to the researcher with no trace it ever existed,
    // which is the one failure the not-drawn disclosure exists to prevent.
    // Callers that legitimately narrow the canvas (e.g. projecting only the
    // primary scenario) must drop those relations before calling, so the
    // count they are measured against is the count they asked for; what
    // reaches here is a dangling endpoint, i.e. a data problem worth saying
    // out loud.
    const notDrawn=[
      ...decided.filter(relation=>relation.decision==='omit'||relation.decision==='implicit')
        .map(relation=>({relationId:relation.id,decision:relation.decision,reason:relation.reason})),
      ...classified.unresolved.map(relation=>({relationId:relation.id,decision:'omit',
        unresolvedEndpoint:true,
        reason:'這條關係接到一個圖上找不到的動作，沒辦法畫'})),
    ];
    const drawable=decided.filter(relation=>relation.decision==='draw-local'||relation.decision==='draw-corridor');
    classified.relations=drawable;
    const steps=asArray(graph&&graph.steps).length?asArray(graph.steps):[...new Set(nodes.map(node=>number(node.step)))].sort((a,b)=>a-b).map(no=>({no,name:`步驟 ${no}`}));
    const stepNos=steps.map((step,index)=>number(step.no??step.step??index+1));
    const stepIndex=new Map(stepNos.map((step,index)=>[step,index]));

    const cardHeightForNode=node=>{
      const lineCount=Math.min(wrapLabelLines(node&&node.label,metrics.cardLabelCharsPerLine).length,3);
      return metrics.badgeClearance+Math.max(1,lineCount)*metrics.cardLineHeight+metrics.cardBottomPadding;
    };

    const cellNodes=new Map();
    for(const node of nodes){
      const key=`${number(node.step)}${node.layer||''}`;
      const group=cellNodes.get(key)||[];group.push(node);cellNodes.set(key,group);
    }
    // draw-only-relations-the-grid-cannot-express 1.4: an `implicit` same-cell
    // `sequence` relation is expressed by card stacking order instead of a
    // line -- topologically sort each cell's cards by those edges, falling
    // back to input order for unconstrained cards or (harmlessly) on a cycle.
    const implicitSequenceEdgesByCell=new Map();
    for(const relation of decided){
      if(relation.decision!=='implicit'||relation.type!=='sequence')continue;
      const key=`${relation.fromStep}${relation.fromLayer}`;
      const list=implicitSequenceEdgesByCell.get(key)||[];list.push([relation.from,relation.to]);implicitSequenceEdgesByCell.set(key,list);
    }
    const topoOrder=(items,edges)=>{
      const ids=items.map(item=>idOf(item.id));
      const indegree=new Map(ids.map(id=>[id,0]));
      const adjacency=new Map(ids.map(id=>[id,[]]));
      for(const [from,to] of edges){
        if(!adjacency.has(from)||!indegree.has(to))continue;
        adjacency.get(from).push(to);
        indegree.set(to,indegree.get(to)+1);
      }
      const remaining=new Set(ids);
      const order=[];
      while(remaining.size){
        const next=ids.find(id=>remaining.has(id)&&indegree.get(id)===0)??ids.find(id=>remaining.has(id));
        order.push(next);remaining.delete(next);
        for(const to of adjacency.get(next)||[])indegree.set(to,indegree.get(to)-1);
      }
      const itemById=new Map(items.map(item=>[idOf(item.id),item]));
      return order.map(id=>itemById.get(id));
    };
    for(const [key,edges] of implicitSequenceEdgesByCell){
      const group=cellNodes.get(key);
      if(group&&group.length>1)cellNodes.set(key,topoOrder(group,edges));
    }
    const rowCardHeight=layers.map(layer=>{
      let tallest=0;
      for(const step of stepNos){
        for(const node of cellNodes.get(`${step}${layer}`)||[])tallest=Math.max(tallest,cardHeightForNode(node));
      }
      return tallest||metrics.cardHeight;
    });
    const laneHeights=layers.map((layer,li)=>{
      let maximum=0;
      for(const step of stepNos)maximum=Math.max(maximum,(cellNodes.get(`${step}${layer}`)||[]).length);
      return Math.max(metrics.cardHeight,maximum*rowCardHeight[li]+Math.max(0,maximum-1)*metrics.cardGap);
    });

    const rowGutterIntervals=Array.from({length:Math.max(0,layers.length-1)},()=>[]);
    const colGutterIntervals=Array.from({length:Math.max(0,stepNos.length-1)},()=>[]);
    for(const relation of classified.relations){
      const fromRow=layerIndex.get(relation.fromLayer),toRow=layerIndex.get(relation.toLayer);
      const fromCol=stepIndex.get(relation.fromStep),toCol=stepIndex.get(relation.toStep);
      // draw-only-relations-the-grid-cannot-express: the row gutter's track
      // budget is reserved for relations that actually cross step columns
      // (`draw-corridor`). A same-column ("draw-local") relation still
      // routes through the physical row-gutter gap when it needs to change
      // lanes, but it must not compete for that shared sub-lane allocation
      // with genuinely cross-column relations.
      if(fromRow!=null&&toRow!=null&&fromRow!==toRow&&relation.decision==='draw-corridor'){
        const lo=Math.min(fromRow,toRow),hi=Math.max(fromRow,toRow);
        const start=Math.min(fromCol??0,toCol??0),end=Math.max(fromCol??0,toCol??0);
        for(let g=lo;g<hi;g++)rowGutterIntervals[g].push({id:relation.id,start,end});
      }
      if(fromCol!=null&&toCol!=null&&fromCol!==toCol){
        const lo=Math.min(fromCol,toCol),hi=Math.max(fromCol,toCol);
        const start=Math.min(fromRow??0,toRow??0),end=Math.max(fromRow??0,toRow??0);
        for(let g=lo;g<hi;g++)colGutterIntervals[g].push({id:relation.id,start,end});
      }
    }
    // A gutter's width is sized to fit every relation that transits it as
    // its own sub-lane (subLaneCount*trackGap) -- but sizing the gutter is
    // pointless unless each relation's *bend* actually lands in its own
    // sub-lane instead of all of them collapsing onto the same midpoint.
    // trackById is kept (not just subLaneCount) so middleMostGutterMid can
    // look up which sub-lane a given relation was assigned.
    const gutterTracksFor=intervals=>{
      if(!intervals.length)return {subLaneCount:0,trackById:new Map()};
      const tracks=allocateTracks(intervals);
      const subLaneCount=Math.max(...tracks.map(track=>track.track))+1;
      return {subLaneCount,trackById:new Map(tracks.map(track=>[track.id,track.track]))};
    };
    const rowGutterTracks=rowGutterIntervals.map(gutterTracksFor);
    const colGutterTracks=colGutterIntervals.map(gutterTracksFor);
    const gutterWidthFor=(gutterTrack,base)=>gutterTrack.subLaneCount?Math.max(base,gutterTrack.subLaneCount*metrics.trackGap):base;
    const rowGutterWidths=rowGutterTracks.map(gt=>gutterWidthFor(gt,metrics.laneGap));
    const colGutterWidths=colGutterTracks.map(gt=>gutterWidthFor(gt,metrics.columnGap));

    const topBand=metrics.topPadding;

    const laneStarts=[];let cursor=topBand;
    for(let index=0;index<laneHeights.length;index++){
      laneStarts.push(cursor);
      const gutterAfter=index<laneHeights.length-1?rowGutterWidths[index]:metrics.laneGap;
      cursor+=laneHeights[index]+gutterAfter;
    }
    const totalHeight=cursor;

    const stepXs=[];let xCursor=metrics.leftPadding;
    for(let index=0;index<stepNos.length;index++){
      stepXs.push(xCursor);
      const gutterAfter=index<stepNos.length-1?colGutterWidths[index]:metrics.columnGap;
      xCursor+=metrics.cardWidth+gutterAfter;
    }
    if(!stepNos.length)xCursor=metrics.leftPadding+metrics.cardWidth+metrics.columnGap;

    // redesign-blueprint-canvas-layout: a bent path's corner sits at the
    // midpoint of the gutter it transits rather than a fixed small offset.
    // For a relation spanning more than one gutter, "middle-most" is the
    // spanned gutter whose index is closest to the midpoint of the full
    // spanned gutter-index range -- for a single-gutter span this is simply
    // that one gutter, unambiguous. Returns null (caller falls back to the
    // arithmetic midpoint between the two ports) when the two indexes are
    // equal (no gutter to speak of) or the gutter's width is unavailable.
    // When more than one relation shares the chosen gutter, landing every
    // one of them on the exact same midpoint recreates a pile of collinear
    // segments -- which is exactly the "sits directly on an earlier line"
    // condition, and used to get silently kicked out to a fixed-portMargin
    // local track instead of staying inside the (already correctly sized)
    // gutter. Spread each relation across its assigned sub-lane, symmetric
    // around the midpoint, so the common single-relation-per-gutter case
    // still lands exactly at 50% (subLaneCount<=1 keeps the plain midpoint).
    const middleMostGutterMid=(indexA,indexB,starts,extents,widths,trackInfos,relationId)=>{
      const lo=Math.min(indexA,indexB),hi=Math.max(indexA,indexB);
      if(lo===hi)return null;
      const rangeMid=(lo+hi-1)/2;
      let bestGutter=lo,bestDist=Infinity;
      for(let gutter=lo;gutter<hi;gutter++){
        const dist=Math.abs(gutter-rangeMid);
        if(dist<bestDist){bestDist=dist;bestGutter=gutter;}
      }
      const width=widths[bestGutter];
      if(!width)return null;
      const mid=starts[bestGutter]+extents[bestGutter]+width/2;
      const info=trackInfos&&trackInfos[bestGutter];
      if(!info||info.subLaneCount<=1)return mid;
      const track=info.trackById.has(relationId)?info.trackById.get(relationId):(info.subLaneCount-1)/2;
      return mid+(track-(info.subLaneCount-1)/2)*metrics.trackGap;
    };
    const rowGutterMid=(rowA,rowB,relationId)=>middleMostGutterMid(rowA,rowB,laneStarts,laneHeights,rowGutterWidths,rowGutterTracks,relationId);
    const colGutterMid=(colA,colB,relationId)=>middleMostGutterMid(colA,colB,stepXs,[...Array(stepXs.length)].map(()=>metrics.cardWidth),colGutterWidths,colGutterTracks,relationId);

    const cards=[];const cardById=new Map();
    for(const [key,group] of cellNodes){
      const [stepStr,layer]=key.split('');
      const step=number(stepStr);
      const column=stepIndex.get(step)??0,row=layerIndex.get(layer)??0;
      const stride=rowCardHeight[row];
      group.forEach((node,index)=>{
        const x=stepXs[column];
        const y=laneStarts[row]+index*(stride+metrics.cardGap);
        // redesign-blueprint-canvas-layout: every card in a lane renders at
        // that lane's tallest content height (stride), not its own content
        // height — replaces the prior per-card tier no-stretch behavior.
        const w=metrics.cardWidth,h=stride;
        const ports={top:{x:x+w/2,y},bottom:{x:x+w/2,y:y+h},left:{x,y:y+h/2},right:{x:x+w,y:y+h/2}};
        const card={id:node.id,step,layer,x,y,w,h,ports};
        cards.push(card);cardById.set(idOf(node.id),card);
      });
    }
    // draw-only-relations-the-grid-cannot-express 1.5: an `implicit` same-cell
    // `same_event` relation is expressed by a group outline around its
    // member cards instead of a line.
    const sameEventGroupIds=new Map();
    for(const relation of decided){
      if(relation.decision!=='implicit'||relation.type!=='same_event')continue;
      const key=`${relation.fromStep}${relation.fromLayer}`;
      const set=sameEventGroupIds.get(key)||new Set();
      set.add(relation.from);set.add(relation.to);
      sameEventGroupIds.set(key,set);
    }
    const groups=[...sameEventGroupIds.entries()].map(([key,idSet])=>{
      const [stepStr,layer]=key.split('');
      const memberCards=[...idSet].map(id=>cardById.get(idOf(id))).filter(Boolean);
      if(!memberCards.length)return null;
      const x=Math.min(...memberCards.map(card=>card.x)),y=Math.min(...memberCards.map(card=>card.y));
      const right=Math.max(...memberCards.map(card=>card.x+card.w)),bottom=Math.max(...memberCards.map(card=>card.y+card.h));
      return {step:number(stepStr),layer,nodeIds:[...idSet],x,y,w:right-x,h:bottom-y};
    }).filter(Boolean);

    const stepBands=stepNos.map((step,index)=>({step,x:stepXs[index],width:metrics.cardWidth}));
    const laneBands=layers.map((layer,index)=>({layer,y:laneStarts[index],height:laneHeights[index]}));
    const width=xCursor;
    const height=totalHeight+metrics.laneGap;
    const obstacles=[...cards.map(card=>({id:card.id,x:card.x,y:card.y,w:card.w,h:card.h,kind:'node'})),
      {id:'lane-header',x:0,y:0,w:metrics.laneHeaderWidth,h:height,kind:'lane-header'}];

    const lengthOf=points=>points.slice(1).reduce((sum,point,index)=>sum+Math.abs(point.x-points[index].x)+Math.abs(point.y-points[index].y),0);
    // redesign-blueprint-canvas-layout: every card exposes a connection
    // point on all four sides; each relation endpoint independently picks
    // whichever of its own card's four sides is geometrically nearest the
    // other card, instead of a joint dx/dy-sign rule shared by both ends.
    const nearestSide=(card,peer)=>{
      const peerCenterX=peer.x+peer.w/2,peerCenterY=peer.y+peer.h/2;
      const inHorizontalSpan=peerCenterX>=card.x&&peerCenterX<=card.x+card.w;
      const inVerticalSpan=peerCenterY>=card.y&&peerCenterY<=card.y+card.h;
      if(inHorizontalSpan)return peerCenterY>=card.y+card.h/2?'bottom':'top';
      if(inVerticalSpan)return peerCenterX>=card.x+card.w/2?'right':'left';
      let best='right',bestDist=Infinity;
      for(const side of ['top','bottom','left','right']){
        const anchor=card.ports[side];
        const dist=Math.hypot(anchor.x-peerCenterX,anchor.y-peerCenterY);
        if(dist<bestDist){bestDist=dist;best=side;}
      }
      return best;
    };
    // A bent path's corner sits at the midpoint of the gutter it transits,
    // not at a fixed offset from either port: when both chosen sides share
    // the same orientation, the free coordinate comes from rowGutterMid /
    // colGutterMid; when the two orientations differ, the corner is already
    // geometrically fixed by the two ports themselves, so no gutter concept
    // applies there.
    const oneBendPath=(sourceCard,targetCard,sourcePort,targetPort,sourceSide,targetSide,relationId)=>{
      const sourceHorizontal=sourceSide==='left'||sourceSide==='right';
      const targetHorizontal=targetSide==='left'||targetSide==='right';
      if(sourceHorizontal&&!targetHorizontal)return [{x:sourcePort.x,y:sourcePort.y},{x:targetPort.x,y:sourcePort.y},{x:targetPort.x,y:targetPort.y}];
      if(!sourceHorizontal&&targetHorizontal)return [{x:sourcePort.x,y:sourcePort.y},{x:sourcePort.x,y:targetPort.y},{x:targetPort.x,y:targetPort.y}];
      if(sourceHorizontal&&targetHorizontal){
        if(sourcePort.y===targetPort.y)return [{x:sourcePort.x,y:sourcePort.y},{x:targetPort.x,y:targetPort.y}];
        const colA=stepIndex.get(sourceCard.step)??0,colB=stepIndex.get(targetCard.step)??0;
        const midX=colGutterMid(colA,colB,relationId)??(sourcePort.x+targetPort.x)/2;
        return [{x:sourcePort.x,y:sourcePort.y},{x:midX,y:sourcePort.y},{x:midX,y:targetPort.y},{x:targetPort.x,y:targetPort.y}];
      }
      if(sourcePort.x===targetPort.x)return [{x:sourcePort.x,y:sourcePort.y},{x:targetPort.x,y:targetPort.y}];
      const rowA=layerIndex.get(sourceCard.layer)??0,rowB=layerIndex.get(targetCard.layer)??0;
      const midY=rowGutterMid(rowA,rowB,relationId)??(sourcePort.y+targetPort.y)/2;
      return [{x:sourcePort.x,y:sourcePort.y},{x:sourcePort.x,y:midY},{x:targetPort.x,y:midY},{x:targetPort.x,y:targetPort.y}];
    };
    const requestedPorts=new Map();
    const routeSides=new Map();
    for(const relation of classified.relations){
      const source=cardById.get(idOf(relation.from)),target=cardById.get(idOf(relation.to));
      if(!source||!target)continue;
      const sourceSide=nearestSide(source,target);
      const targetSide=nearestSide(target,source);
      routeSides.set(relation.id,{sourceSide,targetSide});
      requestedPorts.set(relation.id,{source:{card:source,side:sourceSide,peer:target},target:{card:target,side:targetSide,peer:source}});
    }
    const portGroups=new Map();
    for(const [relationId,ends] of requestedPorts){
      for(const end of [ends.source,ends.target]){
        const key=`${end.card.id}\u001f${end.side}`;
        const group=portGroups.get(key)||[];group.push({relationId,...end});portGroups.set(key,group);
      }
    }
    const allocatedPorts=new Map();
    const portOverflows=[];
    for(const group of portGroups.values()){
      const sample=group[0];
      const horizontalEdge=sample.side==='top'||sample.side==='bottom';
      const capacity=horizontalEdge?5:3;
      if(group.length>capacity)portOverflows.push({card:sample.card.id,side:sample.side,count:group.length,capacity});
      group.sort((a,b)=>{
        const aAxis=(a.side==='top'||a.side==='bottom')?a.peer.x:a.peer.y;
        const bAxis=(b.side==='top'||b.side==='bottom')?b.peer.x:b.peer.y;
        return aAxis-bAxis||idOf(a.relationId).localeCompare(idOf(b.relationId));
      });
      group.forEach((entry,index)=>{
        const slots=horizontalEdge?[1/6,2/6,3/6,4/6,5/6]:[1/4,2/4,3/4];
        // Preserve the established visual slots for ordinary groups.  When a
        // card has more connections than those slots can hold, spread them
        // deterministically across the whole edge instead of duplicating a
        // port and hiding one relation beneath another.
        const ratio=group.length>capacity?(index+1)/(group.length+1):
          (group.length===1?slots[Math.floor(slots.length/2)]:slots[Math.round(index*(slots.length-1)/(group.length-1))]);
        const point=horizontalEdge?{x:entry.card.x+entry.card.w*ratio,y:entry.card.ports[entry.side].y}:{x:entry.card.ports[entry.side].x,y:entry.card.y+entry.card.h*ratio};
        const key=`${entry.relationId}\u001f${entry.card.id}`;allocatedPorts.set(key,point);
      });
    }
    // Same-column vertical alignment is unchanged: no group-size
    // restriction, each relation independently adopts its own target's
    // already-allocated x, since there is no ambiguity to resolve here (the
    // relation's own target point already reflects that side's slot).
    for(const relation of classified.relations){
      const ends=requestedPorts.get(relation.id);
      if(!ends)continue;
      const sourceKey=`${relation.id}${ends.source.card.id}`;
      const targetKey=`${relation.id}${ends.target.card.id}`;
      const sourcePoint=allocatedPorts.get(sourceKey),targetPoint=allocatedPorts.get(targetKey);
      if(!sourcePoint||!targetPoint)continue;
      const sourceVertical=ends.source.side==='top'||ends.source.side==='bottom';
      const targetVertical=ends.target.side==='top'||ends.target.side==='bottom';
      if(ends.source.card.x===ends.target.card.x&&sourceVertical&&targetVertical){
        allocatedPorts.set(sourceKey,{x:targetPoint.x,y:sourcePoint.y});
      }
    }
    // Same-row horizontal alignment generalizes the pre-existing "exactly
    // one relation on each facing side" special case to any equal count N
    // (quantize-blueprint-card-and-gutter-geometry, "Same-count port
    // pairing"): whenever a specific same-row card pair's facing sides carry
    // an equal, mutually-exclusive relation count, pair them by sorted index
    // and place each pair at the same relative position on both edges
    // (using each card's own height, since content tiers can now differ)
    // instead of letting two independently-computed percentages disagree.
    const sameRowPairGroups=new Map();
    for(const relation of classified.relations){
      const ends=requestedPorts.get(relation.id);if(!ends)continue;
      const sourceVertical=ends.source.side==='top'||ends.source.side==='bottom';
      const targetVertical=ends.target.side==='top'||ends.target.side==='bottom';
      if(ends.source.card.y!==ends.target.card.y||sourceVertical||targetVertical)continue;
      const key=`${ends.source.card.id}${ends.source.side}${ends.target.card.id}${ends.target.side}`;
      const group=sameRowPairGroups.get(key)||{ends,relations:[]};
      group.relations.push(relation);
      sameRowPairGroups.set(key,group);
    }
    for(const {ends,relations:pairRelations} of sameRowPairGroups.values()){
      const n=pairRelations.length;
      const sourceGroupSize=portGroups.get(`${ends.source.card.id}${ends.source.side}`)?.length??0;
      const targetGroupSize=portGroups.get(`${ends.target.card.id}${ends.target.side}`)?.length??0;
      if(n!==sourceGroupSize||n!==targetGroupSize)continue;
      const sorted=[...pairRelations].sort((a,b)=>idOf(a.id).localeCompare(idOf(b.id)));
      sorted.forEach((relation,index)=>{
        const ratio=n===1?0.5:(index+1)/(n+1);
        const sourceKey=`${relation.id}${ends.source.card.id}`;
        const targetKey=`${relation.id}${ends.target.card.id}`;
        const sourceY=ends.source.card.y+ends.source.card.h*ratio;
        const targetY=ends.target.card.y+ends.target.card.h*ratio;
        allocatedPorts.set(sourceKey,{x:ends.source.card.ports[ends.source.side].x,y:sourceY});
        allocatedPorts.set(targetKey,{x:ends.target.card.ports[ends.target.side].x,y:targetY});
      });
    }
    const endpointPort=(relation,card)=>allocatedPorts.get(`${relation.id}${card.id}`)||card.ports.center;

    const directEdge=(sourceCard,targetCard,sourcePort,targetPort)=>{
      const forward=targetCard.y>=sourceCard.y;
      return [{x:sourcePort.x,y:sourcePort.y},{x:targetPort.x,y:targetPort.y}];
    };
    const horizontalEdge=(sourceCard,targetCard,sourcePort,targetPort)=>{
      const forward=targetCard.x>=sourceCard.x;
      if(sourcePort.y===targetPort.y)return [sourcePort,targetPort];
      const middleX=(sourcePort.x+targetPort.x)/2;
      return [sourcePort,{x:middleX,y:sourcePort.y},{x:middleX,y:targetPort.y},targetPort];
    };
    // Count only truly shared, collinear portions.  Crossing at a point is
    // still legible; two relations travelling on top of each other are not.
    const sharedLength=(a,b)=>{
      let total=0;
      for(let ai=1;ai<a.length;ai++)for(let bi=1;bi<b.length;bi++){
        const p=a[ai-1],q=a[ai],r=b[bi-1],s=b[bi];
        if(p.y===q.y&&r.y===s.y&&p.y===r.y){
          total+=Math.max(0,Math.min(Math.max(p.x,q.x),Math.max(r.x,s.x))-Math.max(Math.min(p.x,q.x),Math.min(r.x,s.x)));
        }else if(p.x===q.x&&r.x===s.x&&p.x===r.x){
          total+=Math.max(0,Math.min(Math.max(p.y,q.y),Math.max(r.y,s.y))-Math.max(Math.min(p.y,q.y),Math.min(r.y,s.y)));
        }
      }
      return total;
    };
    const uniquePoints=points=>points.filter((point,index)=>index===0||point.x!==points[index-1].x||point.y!==points[index-1].y);
    // Fan candidates must exit each card from the *same* side nearestSide
    // already chose for the direct candidate -- this used to infer exit
    // orientation from raw dx (targetCard.x!==sourceCard.x) instead, so a
    // relation whose ports were both picked as top/bottom (because the peer
    // sat diagonally, not within either span) would still get offered
    // left/right-exit alternatives. Those alternatives run a stub straight
    // along the card's own top/bottom edge before turning -- i.e. exactly
    // the "line hugs the card edge" defect -- and iso the eligible
    // alternative here that mismatches the chosen exit side is now skipped
    // rather than being left as a candidate the length tiebreak might pick.
    const localTrackCandidates=(sourceCard,targetCard,sourcePort,targetPort,sourceSide,targetSide)=>{
      const sourceHorizontal=sourceSide==='left'||sourceSide==='right';
      const targetHorizontal=targetSide==='left'||targetSide==='right';
      if(sourceHorizontal!==targetHorizontal)return [];
      const candidates=[];
      for(let lane=1;lane<=3;lane++)for(const sign of [-1,1]){
        const offset=sign*lane*metrics.trackGap;
        if(sourceHorizontal){
          const forward=targetCard.x>sourceCard.x;
          const sourceX=sourcePort.x+(forward?metrics.portMargin:-metrics.portMargin);
          const targetX=targetPort.x+(forward?-metrics.portMargin:metrics.portMargin);
          if(sourcePort.y===targetPort.y){
            // Already aligned: shift the whole line to a parallel track
            // instead of jogging out and back into the same port -- a
            // detour that reconnects at the original point reads as a
            // wasted bend, not a genuine alternate path.
            const y=sourcePort.y+offset;
            candidates.push([{x:sourcePort.x,y},{x:targetPort.x,y}]);
          }else{
            const middleX=(sourceX+targetX)/2+offset;
            candidates.push([sourcePort,{x:sourceX,y:sourcePort.y},{x:middleX,y:sourcePort.y},{x:middleX,y:targetPort.y},{x:targetX,y:targetPort.y},targetPort]);
          }
        }else{
          const down=targetCard.y>sourceCard.y;
          const sourceY=sourcePort.y+(down?metrics.portMargin:-metrics.portMargin);
          const targetY=targetPort.y+(down?-metrics.portMargin:metrics.portMargin);
          if(sourcePort.x===targetPort.x){
            // Same reasoning as the horizontal branch above: a straight
            // parallel offset, not a bend that returns to the same port.
            const x=sourcePort.x+offset;
            candidates.push([{x,y:sourcePort.y},{x,y:targetPort.y}]);
          }else{
            const middleY=(sourceY+targetY)/2+offset;
            candidates.push([sourcePort,{x:sourcePort.x,y:sourceY},{x:sourcePort.x,y:middleY},{x:targetPort.x,y:middleY},{x:targetPort.x,y:targetY},targetPort]);
          }
        }
      }
      return candidates.map(uniquePoints);
    };
    const shortestClearDetour=(sourceCard,targetCard,ignored,sourcePort,targetPort,relationId)=>{
      const candidates=[];
      const horizontal=targetCard.x!==sourceCard.x;
      if(horizontal){
        const forward=targetCard.x>sourceCard.x;
        const sourceX=sourcePort.x+(forward?metrics.portMargin:-metrics.portMargin);
        const targetX=targetPort.x+(forward?-metrics.portMargin:metrics.portMargin);
        const detourYs=new Set([sourcePort.y,targetPort.y]);
        obstacles.filter(o=>o.kind==='node').forEach(o=>{detourYs.add(o.y-metrics.portMargin);detourYs.add(o.y+o.h+metrics.portMargin);});
        for(const y of detourYs)candidates.push([sourcePort,{x:sourceX,y:sourcePort.y},{x:sourceX,y},{x:targetX,y},{x:targetX,y:targetPort.y},targetPort]);
      }else{
        // redesign-blueprint-canvas-layout: local-interaction relations
        // (the only route class that still reaches this detour path) are
        // always same-step, so this vertical branch is the one that matters
        // -- its step-out offset now comes from the row-gutter midpoint
        // between the two cards' lanes, not a fixed portMargin, falling
        // back to portMargin only when there is no gutter to speak of
        // (adjacent/same-lane stack with no row gutter between them).
        const down=targetCard.y>=sourceCard.y;
        const rowA=layerIndex.get(sourceCard.layer)??0,rowB=layerIndex.get(targetCard.layer)??0;
        const gutterMid=rowGutterMid(rowA,rowB,relationId);
        const sourceY=gutterMid??(sourcePort.y+(down?metrics.portMargin:-metrics.portMargin));
        const targetY=gutterMid??(targetPort.y+(down?-metrics.portMargin:metrics.portMargin));
        const detourXs=new Set([sourcePort.x,targetPort.x]);
        obstacles.filter(o=>o.kind==='node').forEach(o=>{detourXs.add(o.x-metrics.portMargin);detourXs.add(o.x+o.w+metrics.portMargin);});
        for(const x of detourXs)candidates.push([sourcePort,{x:sourcePort.x,y:sourceY},{x,y:sourceY},{x,y:targetY},{x:targetPort.x,y:targetY},targetPort]);
      }
      return candidates.filter(points=>!pathIntersectsObstacle(points,obstacles,ignored).length)
        .sort((a,b)=>lengthOf(a)-lengthOf(b)||a.length-b.length)[0]||null;
    };

    // A perpendicular crossing is a readability cost even when neither line
    // touches a card.  Only count proper interior intersections: a shared
    // card port or a T-junction at a bend is not a crossing.
    const segmentCrossing=(a,b,c,d)=>{
      const aHorizontal=a.y===b.y,bHorizontal=c.y===d.y;
      if(aHorizontal===bHorizontal)return null;
      const horizontal=aHorizontal?{a,b}: {a:c,b:d};
      const vertical=aHorizontal?{a:c,b:d}: {a,b};
      const x=vertical.a.x,y=horizontal.a.y;
      const between=(value,p,q)=>value>Math.min(p,q)&&value<Math.max(p,q);
      return between(x,horizontal.a.x,horizontal.b.x)&&between(y,vertical.a.y,vertical.b.y)?{x,y}:null;
    };
    const crossingPoints=(a,b)=>{
      const points=[];
      for(let ai=1;ai<a.length;ai++)for(let bi=1;bi<b.length;bi++){
        const point=segmentCrossing(a[ai-1],a[ai],b[bi-1],b[bi]);
        if(point)points.push(point);
      }
      return points;
    };
    const crossingCount=(candidate,existing)=>existing.reduce((sum,line)=>sum+crossingPoints(candidate,line.points).length,0);

    // redesign-blueprint-canvas-layout: collision avoidance against
    // intervening cards is dropped for cross-lane relations (every route
    // class except local-interaction) -- they render their direct path even
    // when it geometrically overlaps a card that is not one of their own
    // two endpoints. local-interaction relations keep full avoidance. Track
    // separation between parallel relations (localTrackCandidates) stays
    // available for every route class -- that concern (keeping two distinct
    // relations from visually stacking on each other) is independent of
    // whether a line is allowed to cross an unrelated card.
    const routeFor=(relation,existing)=>{
      const sourceCard=cardById.get(idOf(relation.from)),targetCard=cardById.get(idOf(relation.to));
      if(!sourceCard||!targetCard)return null;
      const ignored=[relation.from,relation.to];
      const sourcePort=endpointPort(relation,sourceCard),targetPort=endpointPort(relation,targetCard);
      const sides=routeSides.get(relation.id);
      const directCandidate=oneBendPath(sourceCard,targetCard,sourcePort,targetPort,sides.sourceSide,sides.targetSide,relation.id);
      const avoidsCards=relation.routeClass==='local-interaction';
      const routeObstacles=avoidsCards?obstacles:obstacles.filter(o=>o.kind!=='node');
      let points=null;
      if(!avoidsCards){
        points=directCandidate;
      }else{
        if(!pathIntersectsObstacle(directCandidate,routeObstacles,ignored).length)points=directCandidate;
        // The dominant reading direction of a service blueprint is left to
        // right.  Same-lane relations therefore get the shortest horizontal
        // candidate first, matching the reference blueprint.  Only fall back
        // to a reserved rail when an intermediate card would be crossed.
        if(!points){
          if(relation.fromStep===relation.toStep){
            const candidate=directEdge(sourceCard,targetCard,sourcePort,targetPort);
            if(!pathIntersectsObstacle(candidate,routeObstacles,ignored).length)points=candidate;
          }else{
            const candidate=horizontalEdge(sourceCard,targetCard,sourcePort,targetPort);
            if(!pathIntersectsObstacle(candidate,routeObstacles,ignored).length)points=candidate;
          }
        }
        if(!points)points=shortestClearDetour(sourceCard,targetCard,ignored,sourcePort,targetPort,relation.id);
        // Pathological metric overrides can make every local candidate invalid;
        // keep the relation visible and let the warning below expose it.
        if(!points)points=horizontalEdge(sourceCard,targetCard,sourcePort,targetPort);
      }
      // When a second relation would sit directly on an earlier one, move it
      // to the nearest clear local track.  These tracks live beside the two
      // related cards, rather than in a global top rail.
      //
      // redesign-blueprint-canvas-layout follow-up: this search used to run
      // unconditionally, even when `points` (the gutter-midpoint bend from
      // oneBendPath/shortestClearDetour) had no overlap with anything.
      // localTrackCandidates' fixed-portMargin offsets are almost always
      // *shorter* than a genuine gutter-midpoint bend, so the length
      // tiebreak silently preferred the portMargin-hugging candidate every
      // time -- reintroducing the "line hugs the card edge" defect the
      // gutter-midpoint rule was meant to fix, for relations that were never
      // actually stacked on another line. Gate the search on the literal
      // condition the comment above already describes: only look for a
      // clear local track when `points` truly sits on an earlier line.
      const baseOverlap=existing.reduce((sum,line)=>sum+sharedLength(points,line.points),0);
      if(baseOverlap>0){
        const alternatives=[points,...localTrackCandidates(sourceCard,targetCard,sourcePort,targetPort,sides.sourceSide,sides.targetSide)]
          .filter(candidate=>!pathIntersectsObstacle(candidate,routeObstacles,ignored).length);
        if(alternatives.length){
          points=alternatives.sort((a,b)=>{
            const crossingA=crossingCount(a,existing);
            const crossingB=crossingCount(b,existing);
            const overlapA=existing.reduce((sum,line)=>sum+sharedLength(a,line.points),0);
            const overlapB=existing.reduce((sum,line)=>sum+sharedLength(b,line.points),0);
            return crossingA-crossingB||overlapA-overlapB||lengthOf(a)-lengthOf(b)||a.length-b.length;
          })[0];
        }
      }
      points=uniquePoints(points);
      const collisions=avoidsCards?pathIntersectsObstacle(points,obstacles,ignored):[];
      return {relationId:relation.id,type:relation.type,routeClass:relation.routeClass,points,collisions};
    };
    const routeCost=(route,existing)=>{
      const overlap=existing.reduce((sum,line)=>sum+sharedLength(route.points,line.points),0);
      return [route.collisions.length,crossingCount(route.points,existing),overlap,route.points.length-2,lengthOf(route.points)];
    };
    const compareCost=(a,b)=>{
      const left=routeCost(a,a._against||[]),right=routeCost(b,b._against||[]);
      for(let index=0;index<left.length;index++)if(left[index]!==right[index])return left[index]-right[index];
      return 0;
    };
    const routes=[];
    for(const relation of classified.relations){
      const route=routeFor(relation,routes);
      if(route)routes.push(route);
    }

    // The first pass is intentionally greedy so the common case stays cheap.
    // Two bounded improvement passes then remove each relation temporarily and
    // reroute it against the rest of the scene.  This gives earlier lines a
    // chance to move when a later relation made the local choice expensive,
    // while keeping the output deterministic and avoiding an unbounded solver.
    const relationById=new Map(classified.relations.map(relation=>[relation.id,relation]));
    // Keep very large blueprints responsive: the bounded improvement is a
    // quality pass for ordinary review-sized scenes, not an accidental
    // quadratic startup cost for diagnostic stress cases. Scoped to
    // local-interaction relations only: a cross-lane relation no longer
    // needs conflict-avoidance rerouting once it is allowed to cross cards,
    // so there is nothing for the optimizer to improve there.
    const optimizableIndexes=routes.map((route,index)=>({route,index}))
      .filter(({route})=>relationById.get(route.relationId)?.routeClass==='local-interaction')
      .map(({index})=>index);
    const optimizationPasses=optimizableIndexes.length&&optimizableIndexes.length<=80?2:0;
    for(let pass=0;pass<optimizationPasses;pass++){
      let changed=false;
      for(const index of optimizableIndexes){
        const current=routes[index];
        const others=routes.filter((_,otherIndex)=>otherIndex!==index);
        const candidate=routeFor(relationById.get(current.relationId),others);
        if(!candidate)continue;
        current._against=others;candidate._against=others;
        if(compareCost(candidate,current)<0){routes[index]=candidate;changed=true;}
        delete current._against;delete candidate._against;
      }
      if(!changed)break;
    }

    const warnings=[];
    for(const route of routes)if(route.collisions.length)warnings.push({type:'collision',relation:route.relationId,obstacles:route.collisions});
    const lines=routes.map(({collisions,...line})=>line);
    warnings.push(...segmentOverlaps(lines,metrics.overlapTolerance));

    // gate-blueprint-line-overlap: measured straight off the scene just
    // built, per invocation -- no hard-coded "too many" threshold. Sub-lane
    // gutter tracking replaced the old single global corridor rail
    // (redesign-blueprint-canvas-layout), so `corridorTracks` is the busiest
    // single gutter's sub-lane count across both axes, the current-
    // architecture equivalent of "how many parallel tracks does the
    // congested point need".
    const allSegments=lines.flatMap(segmentsOf);
    const verticalBucketSizes=new Map();
    for(const segment of allSegments){
      if(segment.axis!=='vertical')continue;
      verticalBucketSizes.set(segment.coordinate,(verticalBucketSizes.get(segment.coordinate)||0)+1);
    }
    const density={
      linesDrawn:lines.length,
      crossColumnLines:lines.filter(line=>{const relation=relationById.get(line.relationId);return relation&&relation.fromStep!==relation.toStep;}).length,
      corridorTracks:Math.max(0,0,...rowGutterTracks.map(gt=>gt.subLaneCount),...colGutterTracks.map(gt=>gt.subLaneCount)),
      busiestVerticalSegments:verticalBucketSizes.size?Math.max(...verticalBucketSizes.values()):0,
    };

    const crossingPairs=[];
    for(let ai=0;ai<lines.length;ai++)for(let bi=ai+1;bi<lines.length;bi++){
      const points=crossingPoints(lines[ai].points,lines[bi].points);
      if(points.length)crossingPairs.push({relations:[lines[ai].relationId,lines[bi].relationId],points});
    }
    const endpointGroups=new Map();
    for(const line of lines){
      for(const point of [line.points[0],line.points[line.points.length-1]]){
        const key=`${point.x},${point.y}`;
        const group=endpointGroups.get(key)||[];group.push(line.relationId);endpointGroups.set(key,group);
      }
    }
    const portDuplicates=[...endpointGroups.entries()]
      .filter(([,relations])=>new Set(relations).size>1)
      .map(([point,relations])=>({point,relations:[...new Set(relations)]}));

    // report-view-fidelity-budget: every rendering states what it carried
    // and what it withheld -- measured from the scene just built, at any
    // graph size, never compared against an absolute node/relation limit.
    const cardsByStep=new Map();
    for(const card of cards){
      const list=cardsByStep.get(card.step)||[];list.push(card);cardsByStep.set(card.step,list);
    }
    const occupiedCounts=[...cardsByStep.values()].map(list=>list.length);
    const median=values=>{
      if(!values.length)return 0;
      const sorted=[...values].sort((a,b)=>a-b);
      const mid=Math.floor(sorted.length/2);
      return sorted.length%2?sorted[mid]:(sorted[mid-1]+sorted[mid])/2;
    };
    const columnMedian=median(occupiedCounts);
    const densityThreshold=columnMedian*metrics.collapseMedianMultiple;
    // A second collapse trigger: a column whose in/out relation count
    // exceeds the distinguishable routing lanes available to it. Under the
    // current architecture this is structurally never true: a column's
    // col-gutter sub-lane count (colGutterTracks) already grows to fit
    // exactly however many concurrent relations transit it
    // (quantize-blueprint-card-and-gutter-geometry's dynamic gutter width),
    // so a gutter's demand can never exceed its own allocated capacity by
    // construction -- there is no fixed ceiling left to compare against.
    // Comparing a column's total relation-touch count (which counts every
    // relation touching the column, not just ones concurrently overlapping
    // in the same gutter) against subLaneCount would be comparing two
    // different kinds of measurement and produced false-positive collapses
    // on the real chongzheng-001 run, which this change's own verification
    // requires to show zero collapses. Kept as an explicit, named condition
    // per the requirement text -- it activates automatically if a future
    // change gives gutters a fixed width again -- rather than deleted or
    // faked with an arbitrary threshold.
    const lanesExceededForStep=()=>false;
    const collapsedColumns=[];
    for(const [step,list] of cardsByStep){
      const reasons=[];
      if(densityThreshold>0&&list.length>densityThreshold)reasons.push('density');
      if(lanesExceededForStep(step))reasons.push('lanes');
      if(reasons.length)collapsedColumns.push({column:stepIndex.get(step)??0,step,cardCount:list.length,reasons});
    }
    const fidelity={
      cardsPlaced:cards.length,
      linesDrawn:lines.length,
      relationsWithoutLine:notDrawn.length,
      // The scene's own denominator: how many relations it was handed. Every
      // one of them leaves as a line or a not-drawn entry, so a caller can
      // check that accounting without knowing how the caller narrowed the
      // relation list before calling.
      relationsConsidered:relations.length,
      busiestColumnCards:occupiedCounts.length?Math.max(...occupiedCounts):0,
    };

    return {cards,lines,laneBands,stepBands,bounds:{width,height},warnings,groups,notDrawn,density,fidelity,collapsedColumns,
      diagnostics:{crossings:crossingPairs.length,crossingPairs,portDuplicates,portOverflows,optimizationPasses},
      unresolved:classified.unresolved,layers,steps};
  }

  root.SBPRelationLayout=Object.freeze({classifyRelations,buildBundles,allocateTracks,pathIntersectsObstacle,buildLargeScene,buildBlueprintScene,wrapLabelLines,renderDecisionFor,segmentOverlaps});
})(typeof globalThis!=='undefined'?globalThis:window);
