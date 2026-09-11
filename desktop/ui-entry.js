import {EditorView,basicSetup} from 'codemirror';
import {EditorState} from '@codemirror/state';
import {python} from '@codemirror/lang-python';
import {javascript} from '@codemirror/lang-javascript';
import {json} from '@codemirror/lang-json';
import {markdown} from '@codemirror/lang-markdown';
import {cpp} from '@codemirror/lang-cpp';
import {Terminal} from '@xterm/xterm';
import {FitAddon} from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

const theme=EditorView.theme({'&':{height:'100%',backgroundColor:'#191919',color:'#ddd',fontSize:'14px'},'.cm-scroller':{fontFamily:'Consolas, monospace',overflow:'auto'},'.cm-content':{caretColor:'#eee'},'.cm-gutters':{backgroundColor:'#191919',color:'#6f6f6f',border:'none'},'.cm-activeLine,.cm-activeLineGutter':{backgroundColor:'#252525'},'.cm-selectionBackground':{backgroundColor:'#3c4a5a!important'},'&.cm-focused':{outline:'none'}},{dark:true});
window.AIGentWidgets={
  editor(parent,options){
    const ext=options.path?.split('.').at(-1)?.toLowerCase();
    const language=ext==='py'?python():['js','jsx','ts','tsx'].includes(ext)?javascript({typescript:ext.startsWith('t'),jsx:ext.endsWith('x')}):ext==='json'?json():ext==='md'?markdown():['c','cpp','h','hpp','cs'].includes(ext)?cpp():[];
    const view=new EditorView({parent,state:EditorState.create({doc:options.text||'',extensions:[basicSetup,theme,language,EditorView.updateListener.of(update=>{if(update.docChanged)options.onChange?.(view.state.doc.toString());})]})});
    return {text:()=>view.state.doc.toString(),destroy:()=>view.destroy(),focus:()=>view.focus()};
  },
  terminal(parent,onData){
    const terminal=new Terminal({theme:{background:'#171717',foreground:'#ddd',cursor:'#ccc'},fontSize:13,fontFamily:'Consolas, monospace',convertEol:true,scrollback:10000});
    const fit=new FitAddon();terminal.loadAddon(fit);terminal.open(parent);terminal.onData(onData);
    const resize=new ResizeObserver(()=>{if(parent.clientWidth>0&&parent.clientHeight>0)fit.fit();});resize.observe(parent);
    return {write:text=>terminal.write(text),clear:()=>terminal.clear(),fit:()=>fit.fit(),destroy:()=>{resize.disconnect();terminal.dispose();}};
  }
};
