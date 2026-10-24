//  NOTE: API calls must start with 'api/' in order to serve the app at any URI

'use strict';
import { h, render, useState, useEffect, useRef, html } from  './bundle.js';
import { Icons, Login, Button, Notification, Pagination } from './components.js';

const API_TIMEOUT = 30000;

function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT);

  return fetch(url, {
    ...options,
    signal: controller.signal
  }).finally(() => clearTimeout(timeoutId));
}

const Logo = props => html`<svg class=${props.class} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12.87 12.85"><defs><style>.ll-cls-1{fill:none;stroke:#000;stroke-miterlimit:10;stroke-width:0.5px;}</style></defs><g id="Layer_2" data-name="Layer 2"><g id="Layer_1-2" data-name="Layer 1"><path class="ll-cls-1" d="M12.62,1.82V8.91A1.58,1.58,0,0,1,11,10.48H4a1.44,1.44,0,0,1-1-.37A.69.69,0,0,1,2.84,10l-.1-.12a.81.81,0,0,1-.15-.48V5.57a.87.87,0,0,1,.86-.86H4.73V7.28a.86.86,0,0,0,.86.85H9.42a.85.85,0,0,0,.85-.85V3.45A.86.86,0,0,0,10.13,3,.76.76,0,0,0,10,2.84a.29.29,0,0,0-.12-.1,1.49,1.49,0,0,0-1-.37H2.39V1.82A1.57,1.57,0,0,1,4,.25H11A1.57,1.57,0,0,1,12.62,1.82Z"/><path class="ll-cls-1" d="M10.48,10.48V11A1.58,1.58,0,0,1,8.9,12.6H1.82A1.57,1.57,0,0,1,.25,11V3.94A1.57,1.57,0,0,1,1.82,2.37H8.9a1.49,1.49,0,0,1,1,.37l.12.1a.76.76,0,0,1,.11.14.86.86,0,0,1,.14.47V7.28a.85.85,0,0,1-.85.85H8.13V5.57a.86.86,0,0,0-.85-.86H3.45a.87.87,0,0,0-.86.86V9.4a.81.81,0,0,0,.15.48l.1.12a.69.69,0,0,0,.13.11,1.44,1.44,0,0,0,1,.37Z"/></g></g></svg>`;

function Header({logout, user, setShowSidebar, showSidebar, theme, toggleTheme}) {
  return html`
<div class="bg-white ind-header sticky top-0 z-[48] xw-full border-b py-2 ${showSidebar && 'pl-40'} transition-all duration-300 transform">
  <div class="px-2 w-full py-0 my-0 flex items-center">
    <button type="button" onclick=${ev => setShowSidebar(v => !v)} class="text-slate-400">
      <${Icons.bars3} class="h-6" />
    <//>
    <div class="flex flex-1 gap-x-4 self-stretch lg:gap-x-6">
      <div class="relative flex flex-1"><//>
      <div class="flex items-center gap-x-4 lg:gap-x-6">
        <span class="text-sm text-slate-400">logged in as: ${user}<//>
        <div class="hidden lg:block lg:h-4 lg:w-px lg:bg-gray-200" aria-hidden="true"><//>
        <${Button} title=${theme === 'ind' ? '经典风格' : '工业风格'} icon=${theme === 'ind' ? Icons.desktop : Icons.bolt} onclick=${toggleTheme} />
        <div class="hidden lg:block lg:h-4 lg:w-px lg:bg-gray-200" aria-hidden="true"><//>
        <${Button} title="Logout" icon=${Icons.logout} onclick=${logout} />
      <//>
    <//>
  <//>
<//>`;
};

function Sidebar({url, show, alarmUnread, videoEnabled}) {
  const NavLink = ({title, icon, href, url, badge}) => html`
  <div>
    <a href="#${href}" class="${href == url ? 'bg-slate-50 text-blue-600 group ind-navlink ind-navlink-active' : 'text-gray-700 hover:text-blue-600 hover:bg-gray-50 group ind-navlink'} flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold relative">
      <${icon} class="w-6 h-6"/>
      ${title}
      ${badge > 0 && html`<span class="absolute right-2 top-1/2 -translate-y-1/2 min-w-[1.35rem] px-1 py-0.5 rounded-full bg-red-500 text-white text-xs font-bold text-center leading-4">${badge > 99 ? '99+' : badge}<//>`}
    <///>
  <//>`;
  return html`
<div class="bg-violet-100 hs-overlay hs-overlay-open:translate-x-0
            -translate-x-full transition-all duration-300 transform
            fixed top-0 left-0 bottom-0 z-[60] w-40 bg-white border-r
            border-gray-200 overflow-y-auto scrollbar-y ind-sidebar
            ${show && 'translate-x-0'} right-auto bottom-0">
  <div class="flex flex-col m-4 gap-y-6">
    <div class="flex h-10 shrink-0 items-center gap-x-4 font-bold text-xl text-slate-500 ind-dim">
      <${Logo} class="h-full"/> GD
    <//>
    <div class="flex flex-1 flex-col gap-y-1">
      ${videoEnabled && html`<${NavLink} title="视频设备" icon=${Icons.camera} href="/events" url=${url} />`}
      <${NavLink} title="实时告警" icon=${Icons.exclamationTriangle} href="/alarms" url=${url} badge=${url === '/alarms' ? 0 : alarmUnread} />
    <//>
  <//>
<//>`;
};

// NOTE: Td/Node MUST be defined at module scope, NOT inside Events.
// Defining components inside a parent gives them a new function identity on
// every parent render, which makes Preact's diffing (it matches vnodes by
// type reference) unmount+remount the whole table body — destroying the
// <tr>/<td> DOM and resetting the scroll container's scrollTop to 0.
// At module scope the type reference is stable, so Preact reconciles in place
// and preserves the DOM (and scroll position) across edits.
const Td = props => html`<td class="whitespace-nowrap border-b border-slate-200 py-2 px-4 pr-3 text-sm text-slate-900" style="width:${props.width}px">${props.text}</td>`;

const Node = ({node, isSelected, fields, formatValue, columnWidthsRef, toggleSelectNode}) => html`
<tr class="${isSelected ? 'bg-blue-50' : ''} hover:bg-gray-50 transition-colors cursor-pointer">
  <td class="whitespace-nowrap border-b border-slate-200 py-2 px-4 pr-3 text-sm text-slate-900">
    <input type="checkbox" checked=${isSelected} onclick=${() => toggleSelectNode(node.id)} class="w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500" />
  </td>
  ${fields.map(f => html`<${Td} text=${formatValue(node, f)} width=${columnWidthsRef.current[f.key]} />`)}
<//>`;

function Events({}) {
  const [data, setData] = useState({nodes: [], fields: [], totalItems: 0});
  const [page, setPage] = useState(() => {
    const savedPage = JSON.parse(localStorage.getItem('page'));
    return savedPage && savedPage > 0 ? savedPage : 1;
  });
  const [isOnlineFilter, setIsOnlineFilter] = useState(['0', '1']);
  const [cameraTypeFilter, setCameraTypeFilter] = useState(['1', '2', '3']);
  const [operationFilter, setOperationFilter] = useState(['', '1', '2', '3', '4']);
  const [searchKeyword, setSearchKeyword] = useState('');
  const [debouncedKeyword, setDebouncedKeyword] = useState('');
  const keywordTimerRef = useRef(null);
  const [showOnlineDropdown, setShowOnlineDropdown] = useState(false);
  const [showCameraDropdown, setShowCameraDropdown] = useState(false);
  const [showOperationDropdown, setShowOperationDropdown] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [itemsPerPage, setItemsPerPage] = useState(50);
  const [maxPageSize, setMaxPageSize] = useState(200);
  const columnWidthsRef = useRef({});
  const [isResizing, setIsResizing] = useState(false);
  const [resizeColumn, setResizeColumn] = useState(null);
  const [startX, setStartX] = useState(0);
  const [startWidth, setStartWidth] = useState(0);
  const nodes = data.nodes;
  const fields = data.fields;
  const totalItems = data.totalItems;

  const loadData = (pg, onlineFilter, cameraFilter, opFilter, keyword) => {
    setIsLoading(true);
    setErrorMsg('');
    let url = `api/nodes/get?page=${pg}&pageSize=${itemsPerPage}&t=${Date.now()}`;
    url += `&isOnline=${onlineFilter.join(',')}`;
    url += `&cameraType=${cameraFilter.join(',')}`;
    const mappedOpFilter = opFilter ? opFilter.map(v => v === '' ? '0' : v) : [];
    url += `&operation=${encodeURIComponent(mappedOpFilter.join(','))}`;
    if (keyword) {
      url += `&keyword=${encodeURIComponent(keyword)}`;
    }
    fetchWithTimeout(url, { method: 'GET', cache: 'no-cache', credentials: 'include' })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP error! status: ${r.status}`);
        return r.json();
      })
      .then(r => {
        if (r.config && r.config.defaultPageSize) {
          setItemsPerPage(r.config.defaultPageSize);
        }
        if (r.config && r.config.maxPageSize) {
          setMaxPageSize(r.config.maxPageSize);
        }
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
        if (err.name === 'AbortError') {
          setErrorMsg('请求超时，请检查网络连接');
        } else {
          setErrorMsg('数据加载失败，请稍后重试');
        }
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

  // Debounce search keyword (300ms)
  useEffect(() => {
    if (keywordTimerRef.current) {
      clearTimeout(keywordTimerRef.current);
    }
    keywordTimerRef.current = setTimeout(() => {
      setDebouncedKeyword(searchKeyword);
    }, 300);
    return () => {
      if (keywordTimerRef.current) {
        clearTimeout(keywordTimerRef.current);
      }
    };
  }, [searchKeyword]);

  // When debounced keyword changes, jump to page 1
  useEffect(() => {
    setPage(1);
    localStorage.removeItem('page');
  }, [debouncedKeyword]);

  // Use debouncedKeyword as dependency
  const keywordKey = debouncedKeyword;

  useEffect(() => {
    loadData(page, isOnlineFilter, cameraTypeFilter, operationFilter, debouncedKeyword);
  }, [page, onlineKey, cameraKey, operationKey, keywordKey]);



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
    setBatchValue('');
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
        <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 ind-th py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
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
        <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 ind-th py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
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
        <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 ind-th py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
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
      <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 ind-th py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter relative" style="width:${colWidth}px">
        ${props.title}
        <div class="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-400 z-20" onmousedown=${(e) => startResize(e, props.fieldKey, colWidth)}></div>
      </th>
    `;
  };

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
    // operation 为 1/2/3 时自动清空 customOperation（仅 4=自定义 才有自定义内容）
    if (value === '1' || value === '2' || value === '3') {
      setEditedCustomOperations(prev => ({
        ...prev,
        [nodeId]: ''
      }));
    }
    setHasPendingChanges(true);
  };

  const handleCustomOperationChange = (nodeId, value) => {
    setEditedCustomOperations(prev => ({
      ...prev,
      [nodeId]: value
    }));
    setHasPendingChanges(true);
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
        // operation 为 1/2/3 时清空 customOperation（仅 4=自定义 才有自定义内容）
        if (batchValue === '1' || batchValue === '2' || batchValue === '3') {
          setEditedCustomOperations(prev => ({
            ...prev,
            [nodeId]: ''
          }));
        }
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
      const op = editedOperations[nodeId];
      const customOp = editedCustomOperations[nodeId];
      const finalOp = op !== undefined ? op : (node ? node.operation : '');
      // operation 为 1/2/3 时 customOperation 必须为空（仅 4=自定义 才有自定义内容）
      const finalCustomOp = (finalOp === '1' || finalOp === '2' || finalOp === '3')
        ? ''
        : (customOp !== undefined ? customOp : (node ? node.customOperation : ''));
      return {
        id: nodeId,
        operation: finalOp,
        customOperation: finalCustomOp
      };
    });

    fetchWithTimeout(`api/nodes/batchset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
      credentials: 'include'
    })
    .then(r => {
      if (!r.ok) throw new Error(`HTTP error! status: ${r.status}`);
      return r.json();
    })
    .then(r => {
      if (r.status === 'true') {
        setEditedOperations({});
        setEditedCustomOperations({});
        setHasPendingChanges(false);
        // 直接更新本地状态，省去 GET 请求
        setData(prev => {
          const newNodes = prev.nodes.map(node => {
            const change = updates.find(u => u.id === node.id);
            if (change) {
              return {
                ...node,
                operation: change.operation,
                customOperation: change.customOperation
              };
            }
            return node;
          });
          return { ...prev, nodes: newNodes };
        });
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
            class="px-2 py-1 border rounded text-sm ${getOperationColor(rawValue)}">
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

return html`
<div class="m-4 ind-panel divide-y divide-gray-200 rounded bg-white flex flex-col flex-1 min-h-0">
  <div class="ind-titlebar">设备通道(${totalItems})</div>
  <div class="font-semibold flex items-center text-gray-600 ind-dim px-3 justify-between whitespace-nowrap border-b border-gray-200 ind-toolbar py-2 flex-shrink-0">
    <div class="font-semibold flex items-center text-gray-600 ind-dim">
      <div class="relative mr-4 flex items-center">
        <input
          type="text"
          value=${searchKeyword}
          oninput=${(e) => setSearchKeyword(e.target.value)}
          placeholder="搜索通道名称或公司名称..."
          class="w-96 px-3 py-1.5 pl-10 ${searchKeyword ? 'pr-10' : 'pr-3'} border border-gray-300 rounded text-sm ind-input focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
        />
        <svg class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 ind-dim" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/>
          <path d="M21 21l-4.35-4.35"/>
        </svg>
        ${searchKeyword && html`
          <button
            type="button"
            onclick=${() => setSearchKeyword('')}
            class="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded ind-dim"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
          </button>
        `}
      </div>
      ${selectedNodes.length > 0 && html`
        <div class="flex items-center gap-2 mr-4">
          <span class="text-sm text-blue-600">已选择 ${selectedNodes.length} 行</span>
          <select 
            value=${batchField} 
            onchange=${(e) => { setBatchField(e.target.value); setBatchValue(''); }}
            class="px-2 py-1 border border-gray-300 rounded text-sm ind-input">
            <option value="">请选择类型</option>
            <option value="operation">操作</option>
            <option value="customOperation">自定义</option>
          </select>
          ${batchField === 'operation' && html`
            <select 
              value=${batchValue} 
              onchange=${(e) => setBatchValue(e.target.value)}
              class="px-2 py-1 border border-gray-300 rounded text-sm ind-input">
              <option value="">请选择值</option>
              <option value="1">重点区域</option>
              <option value="2">高风险作业</option>
              <option value="3">应急值守</option>
            </select>
          `}
          ${batchField === 'customOperation' && html`
            <input 
              type="text" 
              value=${batchValue} 
              oninput=${(e) => setBatchValue(e.target.value)}
              placeholder="请输入自定义内容"
              class="px-2 py-1 border border-gray-300 rounded text-sm ind-input focus:outline-none focus:ring-2 focus:ring-blue-500"
              style="width:180px" />
          `}
          <button 
            onclick=${applyBatchEdit}
            disabled=${!batchValue}
            class="px-3 py-1 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed ind-btn">批量应用</button>
        </div>
      `}
      <button 
        onclick=${saveAllOperations}
        disabled=${Object.keys(editedOperations).length === 0 && Object.keys(editedCustomOperations).length === 0 && !hasPendingChanges}
            class="px-3 py-1 bg-green-600 text-white text-sm rounded hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed">保存全部修改</button>
      ${(Object.keys(editedOperations).length > 0 || Object.keys(editedCustomOperations).length > 0) && html`
        <span class="ml-2 px-2 py-1 bg-yellow-100 text-yellow-800 text-sm rounded font-medium">
          已编辑 ${[...new Set([...Object.keys(editedOperations), ...Object.keys(editedCustomOperations)])].length} 行未保存
        </span>
      `}
    </div>
    <${Pagination} currentPage=${page} setPageFn=${handlePageChange} totalItems=${totalItems} itemsPerPage=${itemsPerPage} />
  <//>
  <div class="flex-1 min-h-0 overflow-auto scrollbar-force bg-white ind-lightbox">
    <table class="border-separate border-spacing-0 bg-white" style="table-layout: auto; width: 100%;">
      <thead>
        <tr>
          <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 ind-th py-1.5 px-4 text-left text-sm font-semibold text-slate-900 backdrop-blur backdrop-filter" style="width:40px">
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
        nodes.map(n => h(Node, {key: n.id, node: n, isSelected: selectedNodes.includes(n.id), fields, formatValue, columnWidthsRef, toggleSelectNode}))}
      </tbody>
    </table>
  <//>
<//>`;
};

// ---- 实时告警页（数据来自 /api/alarms/ws 推送，App 持有 state） ----
// NOTE: AlarmRow 定义在模块作用域，原因见上方 Td/Node 的注释（Preact diffing）。
const ALARM_CAP = 500;  // 前端最多保留的告警条数（超出丢弃最旧）

const AlarmRow = ({a}) => {
  const isAlarm = a.event === 'ALARM';
  return html`
  <tr class="hover:bg-slate-50 ind-dark-row">
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.ts}<//>
    <td class="border-b border-slate-200 py-1.5 px-4">
      <span class="px-2 py-0.5 rounded text-xs font-bold ${isAlarm ? 'bg-red-100 text-red-700 ind-badge-alarm' : 'bg-green-100 text-green-700 ind-badge-recover'}">${a.event}<//>
    <//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 overflow-hidden text-ellipsis whitespace-nowrap" title=${a.region_company_name || ''}>${a.region_company_name || ''}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 overflow-hidden text-ellipsis whitespace-nowrap" title=${a.station_name || ''}>${a.station_name || ''}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm overflow-hidden text-ellipsis whitespace-nowrap" title=${a.device_name ? `${a.device_name}(${a.device_id})` : a.device_id}>
      <span class="text-slate-800">${a.device_name || a.device_id}<//>
      ${a.device_name && html`<span class="text-slate-400 ml-1 text-xs">(${a.device_id})<//>`}
    <//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm overflow-hidden text-ellipsis whitespace-nowrap" title=${a.point_name ? `${a.point_name}(${a.point_code})` : a.point_code}>
      <span class="text-slate-800">${a.point_name || a.point_code}<//>
      ${a.point_name && html`<span class="text-slate-400 ml-1 text-xs">(${a.point_code})<//>`}
    <//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.rule_code}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.temperature != null ? a.temperature.toFixed(1) : '-'}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.temp_rate != null ? a.temp_rate.toFixed(4) : '-'}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.range_1h != null ? a.range_1h.toFixed(2) : '-'}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.range_20s != null ? a.range_20s.toFixed(2) : '-'}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.alarm_type || ''}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 whitespace-nowrap">${a.alarm_value != null ? a.alarm_value.toFixed(2) : '-'}<//>
    <td class="border-b border-slate-200 py-1.5 px-4 text-sm text-slate-600 overflow-hidden text-ellipsis whitespace-nowrap" title=${a.detail || ''}><span>${a.detail || ''}</span></td>
  <//>`;
};

// 模块作用域表格头（稳定 vnode）
const alarmColumns = [
  {label: '时间', width: '150px'},
  {label: '事件', width: '90px'},
  {label: '公司', width: '160px'},
  {label: '场站', width: '140px'},
  {label: '设备', width: '220px'},
  {label: '测点', width: '200px'},
  {label: '规则', width: '110px'},
  {label: '温度(°C)', width: '90px'},
  {label: '温升速率', width: '100px'},
  {label: '1h极差', width: '90px'},
  {label: '20s极差', width: '90px'},
  {label: '告警类型', width: '110px'},
  {label: '告警值', width: '90px'},
  {label: '详情', width: '260px'},
];

// 告警页每页条数（与 data_config.defaultPageSize 对齐）
const ALARM_PAGE_SIZE = 50;

// 告警规则枚举：与 monitor_fj_state.py 的 ALARM_TYPE_MAP 对齐（固定写死）。
// C03_RATE/C04_JUMP 暂未实现，提前占位以兼容未来规则扩展（过滤时无匹配数据即可）。
const ALARM_RULE_OPTIONS = ['C01_FAULT', 'C01_HIGH', 'C01_LOW', 'C02_FROZEN', 'C03_FLUCT', 'C03_RATE', 'C04_JUMP'];
const ALARM_EVENT_OPTIONS = ['ALARM', 'RECOVER'];

function Alarms({items, wsOk, alarmRx, authLost, onReauth}) {
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);       // 服务端告警总数（分页器用）
  const [hist, setHist] = useState([]);        // 历史页数据（page >= 2）
  const [histLoading, setHistLoading] = useState(false);
  const [histError, setHistError] = useState('');
  // 过滤：测点精确匹配（下拉，选项从已见告警累积）+ 规则固定枚举 + 事件类型；跨页生效
  const [pointFilter, setPointFilter] = useState('');
  const [ruleFilter, setRuleFilter] = useState('');
  const [eventFilter, setEventFilter] = useState('');
  const [pointOptions, setPointOptions] = useState([]);  // [{code, name}]
  // 过滤激活时的实时总数增量：仅统计匹配过滤条件的 WS 新行
  const [filteredRx, setFilteredRx] = useState(0);
  const seenMaxIdRef = useRef(0);
  const latestIdRef = useRef(0);
  latestIdRef.current = items.length ? items[0].id : 0;
  // 自动滚动：勾选=新告警实时插入并滚回顶部；取消=冻结当前视图，仅累计提示
  const [autoScroll, setAutoScroll] = useState(() => {
    try { return localStorage.getItem('alarmAutoScroll') !== '0'; } catch (e) { return true; }
  });
  const [frozenMaxId, setFrozenMaxId] = useState(null);  // 冻结时的已见最大 id
  const scrollRef = useRef(null);
  // 总数基数：每次拉取 total 时记下当时的 alarmRx，之后展示 total + 增量差值
  // （wind_alarm 为只追加表，WS 收到的增量行数即总数增量）
  const rxRef = useRef(0);
  rxRef.current = alarmRx;
  const baseRxRef = useRef(0);

  const filterActive = pointFilter !== '' || ruleFilter !== '' || eventFilter !== '';
  const matchFilters = a =>
    (!pointFilter || a.point_code === pointFilter) &&
    (!ruleFilter || a.rule_code === ruleFilter) &&
    (!eventFilter || a.event === eventFilter);
  // 过滤参数串：历史分页与总数查询共用（encodeURIComponent 防特殊字符）
  const filterQuery = () => {
    const q = [];
    if (pointFilter) q.push(`point_code=${encodeURIComponent(pointFilter)}`);
    if (ruleFilter) q.push(`rule_code=${encodeURIComponent(ruleFilter)}`);
    if (eventFilter) q.push(`event=${encodeURIComponent(eventFilter)}`);
    return q.length ? '&' + q.join('&') : '';
  };

  // 测点下拉选项：从 WS 实时列表与历史页响应中累积去重（按编码排序）。
  // 仅"出现过告警的测点"才有意义——未告警的测点过滤结果必然为空。
  useEffect(() => {
    setPointOptions(prev => {
      const map = new Map(prev.map(o => [o.code, o.name]));
      let changed = false;
      for (const src of [items, hist]) {
        for (const a of src) {
          if (a.point_code && !map.has(a.point_code)) {
            map.set(a.point_code, a.point_name || '');
            changed = true;
          }
        }
      }
      if (!changed) return prev;
      return [...map.entries()].map(([code, name]) => ({code, name}))
        .sort((x, y) => x.code.localeCompare(y.code));
    });
  }, [items, hist]);

  // 过滤条件变化：回到第 1 页，重置过滤增量的统计起点
  useEffect(() => {
    setPage(1);
    seenMaxIdRef.current = latestIdRef.current;
    setFilteredRx(0);
  }, [pointFilter, ruleFilter, eventFilter]);

  // 过滤激活时，累加 WS 新行中匹配过滤的行数（items 按 id 倒序，新行在头部）
  useEffect(() => {
    if (!filterActive) return;
    const maxId = latestIdRef.current;
    if (maxId < seenMaxIdRef.current) {
      seenMaxIdRef.current = maxId;  // snapshot 重置等情况：跟随最新基线
      return;
    }
    if (maxId === seenMaxIdRef.current) return;
    let add = 0;
    for (const a of items) {
      if (a.id <= seenMaxIdRef.current) break;
      if (matchFilters(a)) add++;
    }
    seenMaxIdRef.current = maxId;
    if (add > 0) setFilteredRx(c => c + add);
  }, [items, pointFilter, ruleFilter, eventFilter]);

  // 总数：进入页面 / WS 重连成功 / 过滤条件变化时向服务端取一次；失败时用已加载条数兜底
  useEffect(() => {
    fetchWithTimeout(`api/alarms/get?page=1&pageSize=1${filterQuery()}&t=${Date.now()}`, { method: 'GET', cache: 'no-cache', credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then(r => { if (r && typeof r.total === 'number') { baseRxRef.current = rxRef.current; setTotal(r.total); } })
      .catch(() => {});
  }, [wsOk, pointFilter, ruleFilter, eventFilter]);

  // 第 1 页直接用 WS 内存列表（实时）；第 2 页起向服务端按 id 倒序取历史
  useEffect(() => {
    if (page === 1) {
      setHist([]);
      setHistError('');
      return undefined;
    }
    let cancelled = false;
    setHistLoading(true);
    setHistError('');
    fetchWithTimeout(`api/alarms/get?page=${page}&pageSize=${ALARM_PAGE_SIZE}${filterQuery()}&t=${Date.now()}`, { method: 'GET', cache: 'no-cache', credentials: 'include' })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(r => {
        if (cancelled) return;
        setHist(r.items || []);
        if (typeof r.total === 'number') { baseRxRef.current = rxRef.current; setTotal(r.total); }
        setHistLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setHist([]);
        setHistError('历史数据加载失败，请稍后重试');
        setHistLoading(false);
      });
    return () => { cancelled = true; };
  }, [page, pointFilter, ruleFilter, eventFilter]);

  // 自动滚动：items 前插新告警后把滚动条拉回顶部（仅实时页）
  useEffect(() => {
    if (page !== 1 || !autoScroll) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = 0;
  }, [items, page, autoScroll]);

  const toggleAutoScroll = checked => {
    setAutoScroll(checked);
    try { localStorage.setItem('alarmAutoScroll', checked ? '1' : '0'); } catch (e) {}
    if (checked) {
      setFrozenMaxId(null);          // 恢复实时跟随
      const el = scrollRef.current;
      if (el) el.scrollTop = 0;
    } else {
      setFrozenMaxId(items.length ? items[0].id : 0);  // 冻结在当前已见范围
    }
  };

  // 冻结边界：取消自动滚动后，id 大于边界的告警先不插入表格，只累计计数
  const frozenBound = autoScroll
    ? null
    : (frozenMaxId === null ? (items.length ? items[0].id : 0) : frozenMaxId);
  const visibleAll = frozenBound === null ? items : items.filter(x => x.id <= frozenBound);
  // 冻结横幅计数：边界外且匹配过滤条件的新行（无过滤时 matchFilters 恒真）
  const pendingNew = frozenBound === null ? 0
    : items.filter(x => x.id > frozenBound && matchFilters(x)).length;

  const revealNew = () => {
    if (items.length) setFrozenMaxId(items[0].id);
    const el = scrollRef.current;
    if (el) el.scrollTop = 0;
  };

  // 实时页（第 1 页）走内存过滤；历史页已由服务端按相同条件过滤
  const filtered = filterActive ? visibleAll.filter(matchFilters) : visibleAll;
  const rows = page === 1 ? filtered.slice(0, ALARM_PAGE_SIZE) : hist;
  // 实时总数：基数 + 取数后实收的 WS 增量（过滤激活时仅统计匹配行）；
  // 条数下限由内存列表兜底
  const liveTotal = filterActive
    ? total + filteredRx
    : total + Math.max(0, alarmRx - baseRxRef.current);
  const shownTotal = page === 1 ? Math.max(liveTotal, filtered.length) : Math.max(liveTotal, hist.length);
  const latest = items.length > 0 ? items[0].ts : '';
  const showBanner = page === 1 && pendingNew > 0;
  return html`
  <div class="flex-1 min-h-0 flex flex-col p-2">
    <div class="ind-panel flex-1 min-h-0 flex flex-col">
    <div class="ind-titlebar">
      <span class="text-lg font-bold text-slate-700 ind-text">实时告警</span>
      <span class="flex items-center gap-1.5 text-sm ${wsOk ? 'text-green-600 ind-ok' : (authLost ? 'text-amber-600' : 'text-red-500 ind-bad')}">
        <span class="lamp ${wsOk ? 'lamp-green' : 'lamp-red'}"><//>
        ${wsOk ? '已连接' : (authLost ? '登录已失效' : '重连中...')}
      <//>
      ${!wsOk && authLost && html`<button type="button" class="alarm-reauth text-sm px-2 py-0.5 rounded border border-amber-400 text-amber-600 hover:bg-amber-50" onclick=${onReauth}>重新登录<//>`}
      <span class="text-sm ind-dim">共 ${shownTotal} 条</span>
      ${latest && html`<span class="text-sm ind-dim">最新: ${latest}<//>`}
      ${page > 1 && html`<span class="alarm-hist-hint" title="新告警仍会实时进入第 1 页">历史浏览中<//>`}
      <label class="alarm-auto-scroll ${page > 1 ? 'opacity-40' : ''}" title=${page > 1 ? '仅第 1 页实时视图可用' : '勾选：新告警自动滚到顶部；取消：冻结当前列表，只提示新告警数量'}>
        <input type="checkbox" checked=${autoScroll} disabled=${page > 1} onchange=${e => toggleAutoScroll(e.target.checked)} />
        自动滚动
      <//>
    <//>
    <div class="alarm-filterbar">
      <span class="alarm-filter-label">测点</span>
      <select class="alarm-filter-select" value=${pointFilter}
              onchange=${e => setPointFilter(e.target.value)}
              title="按测点编码精确过滤（选项来自已出现的告警）">
        <option value="">全部测点</option>
        ${pointOptions.map(o => html`<option key=${o.code} value=${o.code}>${o.code}${o.name ? ' · ' + o.name : ''}</option>`)}
      </select>
      <span class="alarm-filter-label">规则</span>
      <select class="alarm-filter-select" value=${ruleFilter}
              onchange=${e => setRuleFilter(e.target.value)}
              title="按告警规则精确过滤">
        <option value="">全部规则</option>
        ${ALARM_RULE_OPTIONS.map(r => html`<option key=${r} value=${r}>${r}</option>`)}
      </select>
      <span class="alarm-filter-label">事件</span>
      <select class="alarm-filter-select" value=${eventFilter}
              onchange=${e => setEventFilter(e.target.value)}
              title="按事件类型过滤（ALARM=触发 / RECOVER=恢复）">
        <option value="">全部事件</option>
        ${ALARM_EVENT_OPTIONS.map(ev => html`<option key=${ev} value=${ev}>${ev}</option>`)}
      </select>
      ${filterActive && html`<button type="button" class="alarm-filter-clear"
        onclick=${() => { setPointFilter(''); setRuleFilter(''); setEventFilter(''); }}>清除筛选<//>`}
      ${filterActive && page === 1 && html`<span class="alarm-filter-hint">第 1 页为内存实时过滤，历史页由服务端按相同条件查询<//>`}
    <//>
    ${showBanner && html`<div class="alarm-new-banner" onclick=${revealNew}>↑ ${pendingNew} 条新告警，点击查看<//>`}
    <div class="flex-1 min-h-0 overflow-auto scrollbar-force bg-white ind-lightbox ind-darktable" ref=${scrollRef}>
      <table class="border-separate border-spacing-0 bg-white ind-darktable" style="table-layout: fixed; width: 100%;">
        <thead>
          <tr>
            ${alarmColumns.map(c => html`
              <th scope="col" class="sticky top-0 z-10 border-b border-slate-300 bg-white bg-opacity-75 ind-dark-th py-1.5 px-4 text-left text-sm font-semibold text-slate-900" style=${c.width ? `width:${c.width}` : ''}>${c.label}<//>
            `)}
          <//>
        <//>
        <tbody>
          ${rows.length === 0 ? html`<tr><td colspan="14" class="text-center py-8 text-gray-500">
            ${page === 1
              ? (wsOk ? '暂无告警，等待新事件...' : '未连接告警服务，等待重连...')
              : (histLoading ? '加载中...' : (histError || '本页暂无数据'))}
          <//></tr>` :
          rows.map(a => h(AlarmRow, {key: a.id, a}))}
        <//>
      <//>
    <//>
    ${shownTotal > 0 && html`<${Pagination} currentPage=${page} setPageFn=${setPage} totalItems=${shownTotal} itemsPerPage=${ALARM_PAGE_SIZE} />`}
    <//>
  <//>`;
};

const App = function({}) {
  const [loading, setLoading] = useState(true);
  const [url, setUrl] = useState('/');
  const [user, setUser] = useState('');
  const [showSidebar, setShowSidebar] = useState(true);
  const [videoEnabled, setVideoEnabled] = useState(true);  // 视频设备总开关（来自登录响应）
  // 告警推送状态：items 新->旧，unread 仅统计非 /alarms 页收到的新 ALARM 事件
  const [alarmItems, setAlarmItems] = useState([]);
  const [alarmUnread, setAlarmUnread] = useState(0);
  const [alarmWsOk, setAlarmWsOk] = useState(false);
  // 自上次快照后实收的告警行数（Alarms 用它把总数基数推成实时总数）
  const [alarmRx, setAlarmRx] = useState(0);
  // 服务端重启会使内存会话失效：连续重连失败时探测到 403 就提示重新登录
  const [alarmAuthLost, setAlarmAuthLost] = useState(false);
  // 主题：ind=工业深色 / classic=经典浅色，持久化在 localStorage.theme（初始值由 index.html 头部脚本写到 html class）
  const [theme, setTheme] = useState(() => document.documentElement.classList.contains('ind-theme') ? 'ind' : 'classic');

  const urlRef = useRef(url);
  urlRef.current = url;

  const logout = () => fetchWithTimeout('api/logout', { method: 'POST', credentials: 'include' }).then(r => setUser('')).catch(() => setUser(''));
  const toggleTheme = () => setTheme(prev => {
    const next = prev === 'ind' ? 'classic' : 'ind';
    document.documentElement.classList.toggle('ind-theme', next === 'ind');
    try { localStorage.setItem('theme', next); } catch (e) {}
    return next;
  });
  const login = r => !r.ok ? setLoading(false) && setUser(null) : r.json()
      .then(r => { setUser(r.user); setVideoEnabled(r.videoEnabled !== false); })
      .finally(r => setLoading(false));

  // hash 路由：#/events / #/alarms（Sidebar NavLink 的 href 即锚点）
  useEffect(() => {
    const onHash = () => setUrl(location.hash.slice(1) || '/');
    window.addEventListener('hashchange', onHash);
    onHash();
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // 进入告警页清零未读徽标
  useEffect(() => {
    if (url === '/alarms') setAlarmUnread(0);
  }, [url]);

  // 告警 WS：登录后建连；断线按指数退避重连（2s→30s）；连续失败 3 次时
  // 探测一次会话是否已失效（服务端重启会让内存 session 清空，WS 升级被拒）。
  // 快照(旧->新)倒序入 state；live 增量(旧->新)同样倒序前插。
  useEffect(() => {
    if (!user) return undefined;
    let ws = null;
    let timer = null;
    let disposed = false;
    let failures = 0;
    const connect = () => {
      if (disposed) return;
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${location.host}/api/alarms/ws`);
      ws.onopen = () => {
        failures = 0;
        setAlarmAuthLost(false);
        setAlarmWsOk(true);
      };
      ws.onmessage = ev => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'snapshot') {
          setAlarmItems([...(msg.items || [])].reverse().slice(0, ALARM_CAP));
          setAlarmRx(0);
        } else if (msg.type === 'alarm') {
          const fresh = msg.items || [];
          if (fresh.length === 0) return;
          setAlarmItems(prev => [...fresh].reverse().concat(prev).slice(0, ALARM_CAP));
          setAlarmRx(c => c + fresh.length);
          if (urlRef.current !== '/alarms') {
            const n = fresh.filter(x => x.event === 'ALARM').length;
            if (n > 0) setAlarmUnread(u => u + n);
          }
        }
      };
      ws.onclose = () => {
        setAlarmWsOk(false);
        if (disposed) return;
        failures += 1;
        if (failures === 3) {
          fetchWithTimeout('api/alarms/get?page=1&pageSize=1', { credentials: 'include' })
            .then(r => { if (r.status === 403) setAlarmAuthLost(true); })
            .catch(() => {});
        }
        const delay = Math.min(2000 * Math.pow(2, Math.min(failures - 1, 4)), 30000);
        timer = setTimeout(connect, delay);
      };
      ws.onerror = () => {};
    };
    connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      if (ws) ws.close();
      setAlarmWsOk(false);
      setAlarmAuthLost(false);
    };
  }, [user]);

  useEffect(() => {
    fetchWithTimeout('api/login', { method: 'POST', credentials: 'include' }).then(login).catch(() => { setLoading(false); setUser(null); });
  }, []);

  if (loading) return '';  // Show blank page on initial load
  if (!user) return html`<${Login} loginFn=${login} logoIcon=${Logo}
    title="Device Dashboard Login" />`; // If not logged in, show login screen

  return html`
<div class="h-screen bg-slate-100 ind-shell flex flex-col overflow-hidden">
  <${Sidebar} url=${url} show=${showSidebar} alarmUnread=${alarmUnread} videoEnabled=${videoEnabled} />
  <${Header} logout=${logout} user=${user} showSidebar=${showSidebar} setShowSidebar=${setShowSidebar} theme=${theme} toggleTheme=${toggleTheme} />
  <div class="flex-1 min-h-0 ${showSidebar && 'pl-40'} transition-all duration-300 transform flex flex-col">
    ${url === '/alarms'
      ? html`<${Alarms} items=${alarmItems} wsOk=${alarmWsOk} alarmRx=${alarmRx} authLost=${alarmAuthLost} onReauth=${logout} />`
      : (videoEnabled
          ? html`<${Events} />`
          : html`<div class="flex-1 flex items-center justify-center text-slate-400 text-lg">视频设备功能未启用</div>`)}
  <//>
<//>`;
};

window.onerror = (message, source, lineno, colno, error) => {
  console.error('Global Error:', message, source, lineno, colno, error);
  if (error && error.name === 'AbortError') return;
  try {
    const notification = document.createElement('div');
    notification.className = 'fixed top-4 right-4 bg-red-500 text-white px-4 py-3 rounded-lg shadow-lg z-[100]';
    notification.textContent = '系统发生错误，请刷新页面';
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 5000);
  } catch (e) {
    console.error('Failed to show error notification:', e);
  }
};

window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled Rejection:', event.reason);
  event.preventDefault();
  try {
    const notification = document.createElement('div');
    notification.className = 'fixed top-4 right-4 bg-red-500 text-white px-4 py-3 rounded-lg shadow-lg z-[100]';
    notification.textContent = '请求失败，请检查网络连接';
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 5000);
  } catch (e) {
    console.error('Failed to show error notification:', e);
  }
});

render(h(App), document.body);
