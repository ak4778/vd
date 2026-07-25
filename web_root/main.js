//  NOTE: API calls must start with 'api/' in order to serve the app at any URI

'use strict';
import { h, render, useState, useEffect, useRef, html } from  './bundle.js';
import { Icons, Login, Button, Notification, Pagination } from './components.js';

const Logo = props => html`<svg class=${props.class} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12.87 12.85"><defs><style>.ll-cls-1{fill:none;stroke:#000;stroke-miterlimit:10;stroke-width:0.5px;}</style></defs><g id="Layer_2" data-name="Layer 2"><g id="Layer_1-2" data-name="Layer 1"><path class="ll-cls-1" d="M12.62,1.82V8.91A1.58,1.58,0,0,1,11,10.48H4a1.44,1.44,0,0,1-1-.37A.69.69,0,0,1,2.84,10l-.1-.12a.81.81,0,0,1-.15-.48V5.57a.87.87,0,0,1,.86-.86H4.73V7.28a.86.86,0,0,0,.86.85H9.42a.85.85,0,0,0,.85-.85V3.45A.86.86,0,0,0,10.13,3,.76.76,0,0,0,10,2.84a.29.29,0,0,0-.12-.1,1.49,1.49,0,0,0-1-.37H2.39V1.82A1.57,1.57,0,0,1,4,.25H11A1.57,1.57,0,0,1,12.62,1.82Z"/><path class="ll-cls-1" d="M10.48,10.48V11A1.58,1.58,0,0,1,8.9,12.6H1.82A1.57,1.57,0,0,1,.25,11V3.94A1.57,1.57,0,0,1,1.82,2.37H8.9a1.49,1.49,0,0,1,1,.37l.12.1a.76.76,0,0,1,.11.14.86.86,0,0,1,.14.47V7.28a.85.85,0,0,1-.85.85H8.13V5.57a.86.86,0,0,0-.85-.86H3.45a.87.87,0,0,0-.86.86V9.4a.81.81,0,0,0,.15.48l.1.12a.69.69,0,0,0,.13.11,1.44,1.44,0,0,0,1,.37Z"/></g></g></svg>`;

function Header({logout, user, setShowSidebar, showSidebar}) {
  return html`
<div class="bg-white sticky top-0 z-[48] xw-full border-b py-2 ${showSidebar && 'pl-72'} transition-all duration-300 transform">
  <div class="px-2 w-full py-0 my-0 flex items-center">
    <button type="button" onclick=${ev => setShowSidebar(v => !v)} class="text-slate-400">
      <${Icons.bars3} class="h-6" />
    <//>
    <div class="flex flex-1 gap-x-4 self-stretch lg:gap-x-6">
      <div class="relative flex flex-1"><//>
      <div class="flex items-center gap-x-4 lg:gap-x-6">
        <span class="text-sm text-slate-400">logged in as: ${user}<//>
        <div class="hidden lg:block lg:h-4 lg:w-px lg:bg-gray-200" aria-hidden="true"><//>
        <${Button} title="Logout" icon=${Icons.logout} onclick=${logout} />
      <//>
    <//>
  <//>
<//>`;
};

function Sidebar({url, show}) {
  const NavLink = ({title, icon, href, url}) => html`
  <div>
    <a href="#${href}" class="${href == url ? 'bg-slate-50 text-blue-600 group' : 'text-gray-700 hover:text-blue-600 hover:bg-gray-50 group'} flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold">
      <${icon} class="w-6 h-6"/>
      ${title}
    <///>
  <//>`;
  return html`
<div class="bg-violet-100 hs-overlay hs-overlay-open:translate-x-0
            -translate-x-full transition-all duration-300 transform
            fixed top-0 left-0 bottom-0 z-[60] w-72 bg-white border-r
            border-gray-200 overflow-y-auto scrollbar-y
            ${show && 'translate-x-0'} right-auto bottom-0">
  <div class="flex flex-col m-4 gap-y-6">
    <div class="flex h-10 shrink-0 items-center gap-x-4 font-bold text-xl text-slate-500">
      <${Logo} class="h-full"/> Your Brand
    <//>
    <div class="flex flex-1 flex-col">
      <${NavLink} title="视频设备" icon=${Icons.alert} href="/events" url=${url} />
    <//>
  <//>
<//>`;
};

function Events({}) {
  const [data, setData] = useState({nodes: [], fields: [], totalItems: 0});
  const [page, setPage] = useState(() => {
    const savedPage = JSON.parse(localStorage.getItem('page'));
    return savedPage && savedPage > 0 ? savedPage : 1;
  });
  const [isOnlineFilter, setIsOnlineFilter] = useState(['0', '1']);
  const [cameraTypeFilter, setCameraTypeFilter] = useState(['1', '2', '3']);
  const [operationFilter, setOperationFilter] = useState(['', '1', '2', '3', '4']);
  const [showOnlineDropdown, setShowOnlineDropdown] = useState(false);
  const [showCameraDropdown, setShowCameraDropdown] = useState(false);
  const [showOperationDropdown, setShowOperationDropdown] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const columnWidthsRef = useRef({});
  const [isResizing, setIsResizing] = useState(false);
  const [resizeColumn, setResizeColumn] = useState(null);
  const [startX, setStartX] = useState(0);
  const [startWidth, setStartWidth] = useState(0);
  const itemsPerPage = 50;
  const nodes = data.nodes;
  const fields = data.fields;
  const totalItems = data.totalItems;

  const loadData = (pg, onlineFilter, cameraFilter, opFilter) => {
    setIsLoading(true);
    setErrorMsg('');
    let url = `api/nodes/get?page=${pg}&pageSize=${itemsPerPage}&t=${Date.now()}`;
    if (onlineFilter.length > 0) url += `&isOnline=${onlineFilter.join(',')}`;
    if (cameraFilter.length > 0) url += `&cameraType=${cameraFilter.join(',')}`;
    const mappedOpFilter = opFilter ? opFilter.map(v => v === '' ? '0' : v) : [];
    url += `&operation=${encodeURIComponent(mappedOpFilter.join(','))}`;
    fetch(url, { method: 'GET', cache: 'no-cache' })
      .then(r => r.json())
      .then(r => {
        const newFields = r.config && r.config.fields ? r.config.fields : [];
        newFields.forEach(f => {
          if (f.width && columnWidthsRef.current[f.key] === undefined) {
            columnWidthsRef.current[f.key] = f.width;
          }
        });
        const preservedFields = newFields.map(f => ({
          ...f,
          width: columnWidthsRef.current[f.key]
        }));
        const newData = {
          nodes: r.data && r.data.nodes ? r.data.nodes : [],
          fields: preservedFields,
          totalItems: r.data && r.data.total ? r.data.total : 0
        };
        setData(newData);
        setIsLoading(false);
      })
      .catch(err => {
        console.error('API Error:', err);
        setErrorMsg('数据加载失败，请稍后重试');
        setData({nodes: [], fields: [], totalItems: 0});
        setIsLoading(false);
      });
  };

  const toggleOnlineFilter = (val) => {
    setIsOnlineFilter(prev => {
      return prev.includes(val) ? prev.filter(v => v !== val) : [...prev, val];
    });
    setPage(1);
    localStorage.removeItem('page');
  };

  const toggleCameraTypeFilter = (val) => {
    setCameraTypeFilter(prev => {
      return prev.includes(val) ? prev.filter(v => v !== val) : [...prev, val];
    });
    setPage(1);
    localStorage.removeItem('page');
  };

  const toggleOperationFilter = (val) => {
    setOperationFilter(prev => {
      const newFilter = prev.includes(val) ? prev.filter(v => v !== val) : [...prev, val];
      return newFilter;
    });
    setPage(1);
    localStorage.removeItem('page');
  };

  // 用 JSON.stringify 作为依赖，确保筛选内容变化（不仅仅是数量）时也重新加载
  const onlineKey = JSON.stringify(isOnlineFilter);
  const cameraKey = JSON.stringify(cameraTypeFilter);
  const operationKey = JSON.stringify(operationFilter);

  useEffect(() => {
    loadData(page, isOnlineFilter, cameraTypeFilter, operationFilter);
  }, [page, onlineKey, cameraKey, operationKey]);



  useEffect(() => {
    const handleMouseMove = (e) => {
      if (!isResizing || !resizeColumn) return;
      const deltaX = e.clientX - startX;
      const newWidth = Math.max(50, startWidth + deltaX);
      columnWidthsRef.current[resizeColumn] = newWidth;
      setData(prev => prev); // 触发重新渲染以更新列宽
    };

    const handleMouseUp = () => {
      setIsResizing(false);
      setResizeColumn(null);
    };

    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizing, resizeColumn, startX, startWidth]);

  const startResize = (e, fieldKey, currentWidth) => {
    e.preventDefault();
    setIsResizing(true);
    setResizeColumn(fieldKey);
    setStartX(e.clientX);
    setStartWidth(currentWidth);
  };

  const handlePageChange = (newPage) => {
    setPage(newPage);
    localStorage.setItem('page', newPage.toString());
    setSelectedNodes([]);
    loadData(newPage, isOnlineFilter, cameraTypeFilter, operationFilter);
  };

  useEffect(() => {
    const handleClickOutside = (event) => {
      const target = event.target;
      if (!target.closest('.relative')) {
        setShowOnlineDropdown(false);
        setShowCameraDropdown(false);
        setShowOperationDropdown(false);
      }
    };
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, []);

  const Th = props => {
    const colWidth = columnWidthsRef.current[props.fieldKey] !== undefined 
      ? columnWidthsRef.current[props.fieldKey] 
      : props.width || 80;
    if (props.fieldKey === 'isOnline') {
      return html`
        <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
          <div class="relative">
            <button onClick=${() => { setShowOnlineDropdown(!showOnlineDropdown); setShowCameraDropdown(false); setShowOperationDropdown(false); }} class="flex items-center">
              <span>${props.title}</span>
              <svg class="ml-1 w-4 h-4 text-slate-400 transition-transform" style=${showOnlineDropdown ? 'transform: rotate(180deg)' : ''} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            ${showOnlineDropdown && html`
              <div class="absolute top-full left-0 mt-1 bg-white border border-gray-200 rounded-lg shadow-lg z-20 p-2 min-w-[120px]">
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${isOnlineFilter.includes('1')} onclick=${() => toggleOnlineFilter('1')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-green-600 font-medium">在线</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${isOnlineFilter.includes('0')} onclick=${() => toggleOnlineFilter('0')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-red-600 font-medium">离线</span>
                </label>
              </div>
            `}
          </div>
          <div class="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-400 z-20" onmousedown=${(e) => startResize(e, props.fieldKey, colWidth)}></div>
        </th>
      `;
    }
    if (props.fieldKey === 'cameraType') {
      return html`
        <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
          <div class="relative">
            <button onClick=${() => { setShowCameraDropdown(!showCameraDropdown); setShowOnlineDropdown(false); setShowOperationDropdown(false); }} class="flex items-center">
              <span>${props.title}</span>
              <svg class="ml-1 w-4 h-4 text-slate-400 transition-transform" style=${showCameraDropdown ? 'transform: rotate(180deg)' : ''} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            ${showCameraDropdown && html`
              <div class="absolute top-full left-0 mt-1 bg-white border border-gray-200 rounded-lg shadow-lg z-20 p-2 min-w-[140px]">
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${cameraTypeFilter.includes('1')} onclick=${() => toggleCameraTypeFilter('1')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-blue-600 font-medium">枪机</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${cameraTypeFilter.includes('2')} onclick=${() => toggleCameraTypeFilter('2')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-purple-600 font-medium">球机</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${cameraTypeFilter.includes('3')} onclick=${() => toggleCameraTypeFilter('3')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-orange-600 font-medium">半球</span>
                </label>
              </div>
            `}
          </div>
          <div class="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-400 z-20" onmousedown=${(e) => startResize(e, props.fieldKey, colWidth)}></div>
        </th>
      `;
    }
    if (props.fieldKey === 'operation') {
      return html`
        <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
          <div class="relative">
            <button onClick=${() => { setShowOperationDropdown(!showOperationDropdown); setShowOnlineDropdown(false); setShowCameraDropdown(false); }} class="flex items-center">
              <span>${props.title}</span>
              <svg class="ml-1 w-4 h-4 text-slate-400 transition-transform" style=${showOperationDropdown ? 'transform: rotate(180deg)' : ''} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            ${showOperationDropdown && html`
              <div class="absolute top-full left-0 mt-1 bg-white border border-gray-200 rounded-lg shadow-lg z-20 p-2 min-w-[160px]">
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${operationFilter.includes('')} onclick=${() => toggleOperationFilter('')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-gray-500 font-medium">未标记</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${operationFilter.includes('1')} onclick=${() => toggleOperationFilter('1')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-blue-600 font-medium">重点区域</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${operationFilter.includes('2')} onclick=${() => toggleOperationFilter('2')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-orange-600 font-medium">高风险作业</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${operationFilter.includes('3')} onclick=${() => toggleOperationFilter('3')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-yellow-600 font-medium">应急值守</span>
                </label>
                <label class="flex items-center px-2 py-1 cursor-pointer hover:bg-gray-50">
                  <input type="checkbox" checked=${operationFilter.includes('4')} onclick=${() => toggleOperationFilter('4')} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
                  <span class="ml-2 text-sm text-purple-600 font-medium">自定义</span>
                </label>
              </div>
            `}
          </div>
          <div class="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-400 z-20" onmousedown=${(e) => startResize(e, props.fieldKey, colWidth)}></div>
        </th>
      `;
    }
    return html`
      <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
        ${props.title}
        <div class="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-400 z-20" onmousedown=${(e) => startResize(e, props.fieldKey, colWidth)}></div>
      </th>
    `;
  };
  const Td = props => html`<td class="whitespace-nowrap border-b border-slate-200 py-2 px-4 pr-3 text-sm text-slate-900" style="width:${props.width}px">${props.text}</td>`;

  const [editedOperations, setEditedOperations] = useState({});
  const [editedCustomOperations, setEditedCustomOperations] = useState({});
  const [hasPendingChanges, setHasPendingChanges] = useState(false);
  const [selectedNodes, setSelectedNodes] = useState([]);
  const [batchField, setBatchField] = useState('operation');
  const [batchValue, setBatchValue] = useState('');

  const handleOperationChange = (nodeId, value) => {
    setEditedOperations(prev => ({
      ...prev,
      [nodeId]: value
    }));
  };

  const handleCustomOperationChange = (nodeId, value) => {
    setEditedCustomOperations(prev => ({
      ...prev,
      [nodeId]: value
    }));
  };

  const toggleSelectNode = (nodeId) => {
    setSelectedNodes(prev => {
      if (prev.includes(nodeId)) {
        return prev.filter(id => id !== nodeId);
      } else {
        return [...prev, nodeId];
      }
    });
  };

  const toggleSelectAll = () => {
    if (selectedNodes.length === nodes.length) {
      setSelectedNodes([]);
    } else {
      setSelectedNodes(nodes.map(n => n.id));
    }
  };

  const applyBatchEdit = () => {
    if (selectedNodes.length === 0 || !batchField) return;
    
    selectedNodes.forEach(nodeId => {
      if (batchField === 'operation') {
        setEditedOperations(prev => ({
          ...prev,
          [nodeId]: batchValue
        }));
      } else if (batchField === 'customOperation') {
        setEditedOperations(prev => ({
          ...prev,
          [nodeId]: '4'
        }));
        setEditedCustomOperations(prev => ({
          ...prev,
          [nodeId]: batchValue
        }));
      }
    });
    
    setSelectedNodes([]);
    setBatchValue('');
  };

  const saveAllOperations = () => {
    const changes = Object.keys(editedOperations);
    const customChanges = Object.keys(editedCustomOperations);
    const allChanges = [...new Set([...changes, ...customChanges])];
    if (allChanges.length === 0) return;

    // 一次性发送所有修改
    const updates = allChanges.map(nodeId => {
      const node = data.nodes.find(n => n.id === nodeId);
      return {
        id: nodeId,
        operation: editedOperations[nodeId] || (node ? node.operation : ''),
        customOperation: editedCustomOperations[nodeId] || (node ? node.customOperation : '')
      };
    });

    fetch(`api/nodes/batchset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates })
    })
    .then(r => r.json())
    .then(r => {
      if (r.status === 'true') {
        // 直接更新本地状态，不需要重新请求
        setData(prev => ({
          ...prev,
          nodes: prev.nodes.map(node => {
            if (allChanges.includes(node.id)) {
              return {
                ...node,
                operation: editedOperations[node.id] || node.operation,
                customOperation: editedCustomOperations[node.id] || node.customOperation
              };
            }
            return node;
          })
        }));
        setEditedOperations({});
        setEditedCustomOperations({});
        setHasPendingChanges(false);
      }
    })
    .catch(err => console.error('Save error:', err));
  };

  const operationOptions = [
    { value: '', label: '请选择' },
    { value: '1', label: '重点区域' },
    { value: '2', label: '高风险作业' },
    { value: '3', label: '应急值守' },
    { value: '4', label: '自定义' }
  ];
  
  const operationMap = {
    '': '未标记',
    '1': '重点区域',
    '2': '高风险作业',
    '3': '应急值守',
    '4': '自定义'
  };

  const formatValue = (node, field) => {
    if (field.key === 'isOnline') {
      const isOnline = String(node[field.key]) === '1';
      return html`<span class="flex items-center"><span class="w-2 h-2 rounded-full mr-2 ${isOnline ? 'bg-green-500' : 'bg-gray-400'}"></span>${isOnline ? '在线' : '离线'}</span>`;
    }
    if (field.key === 'cameraType') {
      const types = {'1': '枪机', '2': '球机', '3': '半球', '5': '本地采集输入'};
      const getCameraColor = (val) => {
        switch(val) {
          case '枪机': return 'bg-cyan-100 text-cyan-800';
          case '球机': return 'bg-orange-100 text-orange-800';
          case '半球': return 'bg-teal-100 text-teal-800';
          case '本地采集输入': return 'bg-gray-100 text-gray-800';
          default: return 'bg-white text-gray-700';
        }
      };
      const typeName = types[node[field.key]] || node[field.key] || '-';
      return html`<span class="px-2 py-0.5 rounded text-sm ${getCameraColor(typeName)}">${typeName}</span>`;
    }
    if (field.key === 'operation') {
      const rawValue = editedOperations[node.id] || node[field.key] || '';
      const getOperationColor = (val) => {
        const displayVal = operationMap[val] || val;
        switch(displayVal) {
          case '重点区域': return 'bg-blue-100 text-blue-800 border-blue-300';
          case '高风险作业': return 'bg-red-100 text-red-800 border-red-300';
          case '应急值守': return 'bg-yellow-100 text-yellow-800 border-yellow-300';
          case '自定义': return 'bg-purple-100 text-purple-800 border-purple-300';
          default: return 'bg-white text-gray-700 border-gray-300';
        }
      };
      return html`
        <div class="flex items-center gap-2">
          <select 
            value=${rawValue} 
            onchange=${(e) => handleOperationChange(node.id, e.target.value)}
            class="px-2 py-1 border rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 ${getOperationColor(rawValue)}">
            ${operationOptions.map(opt => html`
              <option value=${opt.value}>${opt.label}</option>
            `)}
          </select>
        </div>
      `;
    }
    if (field.key === 'customOperation') {
      const isCustom = (editedOperations[node.id] || node.operation || '') === '4';
      if (isCustom) {
        return html`
          <input 
            type="text" 
            value=${editedCustomOperations[node.id] || node.customOperation || ''}
            onfocus=${() => setHasPendingChanges(true)}
            onchange=${(e) => handleCustomOperationChange(node.id, e.target.value)}
            placeholder="请输入自定义内容"
            class="px-2 py-1 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            style="width:300px" />
        `;
      }
      return '-';
    }
    return node[field.key] || '-';
  };

  const Node = ({node, isSelected}) => html`
<tr class="${isSelected ? 'bg-blue-50' : ''} hover:bg-gray-50 transition-colors cursor-pointer">
  <td class="whitespace-nowrap border-b border-slate-200 py-2 px-4 pr-3 text-sm text-slate-900">
    <input type="checkbox" checked=${isSelected} onclick=${() => toggleSelectNode(node.id)} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
  </td>
  ${fields.map(f => html`<${Td} text=${formatValue(node, f)} width=${columnWidthsRef.current[f.key]} />`)}
<//>`;

return html`
<div class="m-4 divide-y divide-gray-200 rounded bg-white flex flex-col h-[calc(100vh-120px)]">
  <div class="font-semibold flex items-center text-gray-600 px-3 justify-between whitespace-nowrap border-b border-gray-200 py-2 flex-shrink-0">
    <div class="font-semibold flex items-center text-gray-600">
      <div class="mr-4">设备通道(${totalItems})</div>
      ${selectedNodes.length > 0 && html`
        <div class="flex items-center gap-2 mr-4">
          <span class="text-sm text-blue-600">已选择 ${selectedNodes.length} 行</span>
          <select 
            value=${batchField} 
            onchange=${(e) => { setBatchField(e.target.value); setBatchValue(''); }}
            class="px-2 py-1 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
            <option value="">请选择类型</option>
            <option value="operation">操作</option>
            <option value="customOperation">自定义</option>
          </select>
          ${batchField === 'operation' && html`
            <select 
              value=${batchValue} 
              onchange=${(e) => setBatchValue(e.target.value)}
              class="px-2 py-1 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
              <option value="">请选择值</option>
              <option value="1">重点区域</option>
              <option value="2">高风险作业</option>
              <option value="3">应急值守</option>
              <option value="4">自定义</option>
            </select>
          `}
          ${batchField === 'customOperation' && html`
            <input 
              type="text" 
              value=${batchValue} 
              oninput=${(e) => setBatchValue(e.target.value)}
              placeholder="请输入自定义内容"
              class="px-2 py-1 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              style="width:180px" />
          `}
          <button 
            onclick=${applyBatchEdit}
            disabled=${!batchValue}
            class="px-3 py-1 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed">批量应用</button>
        </div>
      `}
      <button 
        onclick=${saveAllOperations}
        disabled=${Object.keys(editedOperations).length === 0 && Object.keys(editedCustomOperations).length === 0 && !hasPendingChanges}
            class="px-3 py-1 bg-green-600 text-white text-sm rounded hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed">保存全部修改</button>
    </div>
    <${Pagination} currentPage=${page} setPageFn=${handlePageChange} totalItems=${totalItems} itemsPerPage=${itemsPerPage} />
  <//>
  <div class="flex-1 overflow-y-scroll overflow-x-auto scrollbar-force">
    <table class="border-separate border-spacing-0" style="table-layout: fixed; width: 100%;">
      <thead>
        <tr>
          <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter" style="width:40px">
            <input type="checkbox" checked=${nodes.length > 0 && selectedNodes.length === nodes.length} onclick=${toggleSelectAll} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
          </th>
          ${fields.map(f => html`<${Th} title=${f.label} width=${f.width} fieldKey=${f.key} />`)}
        </tr>
      </thead>
      <tbody>
        ${isLoading ? html`<tr><td colspan=${fields.length + 1} class="text-center py-8 text-blue-500">
          <div class="inline-flex items-center gap-2">
            <svg class="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            <span>加载中...</span>
          </div>
        </td></tr>` :
        errorMsg ? html`<tr><td colspan=${fields.length + 1} class="text-center py-8 text-red-500">${errorMsg}</td></tr>` :
        nodes.length === 0 ? html`<tr><td colspan=${fields.length + 1} class="text-center py-8 text-gray-500">暂无数据，请选择过滤条件</td></tr>` :
        nodes.map(n => h(Node, {node: n, isSelected: selectedNodes.includes(n.id)}))}
      </tbody>
    </table>
  <//>
<//>`;
};

const App = function({}) {
  const [loading, setLoading] = useState(true);
  const [url, setUrl] = useState('/');
  const [user, setUser] = useState('');
  const [showSidebar, setShowSidebar] = useState(true);

  const logout = () => fetch('api/logout').then(r => setUser(''));
  const login = r => !r.ok ? setLoading(false) && setUser(null) : r.json()
      .then(r => setUser(r.user))
      .finally(r => setLoading(false));

  useEffect(() => fetch('api/login').then(login), []);

  if (loading) return '';  // Show blank page on initial load
  if (!user) return html`<${Login} loginFn=${login} logoIcon=${Logo}
    title="Device Dashboard Login" 
    tipText="To login, use: admin/admin, user1/user1, user2/user2" />`; // If not logged in, show login screen

  return html`
<div class="min-h-screen bg-slate-100 flex flex-col">
  <${Sidebar} url=${url} show=${showSidebar} />
  <${Header} logout=${logout} user=${user} showSidebar=${showSidebar} setShowSidebar=${setShowSidebar} />
  <div class="flex-1 ${showSidebar && 'pl-72'} transition-all duration-300 transform">
    <${Events} />
  <//>
<//>`;
};

window.onload = () => render(h(App), document.body);
