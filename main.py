# -*- coding: utf-8 -*-
import kivy
import os
import sys
import sqlite3
import oerplib

kivy.require('1.8.0')
from kivy.app import App
from os.path import dirname, join
from kivy.lang import Builder
from kivy.properties import NumericProperty, StringProperty, BooleanProperty,\
    ListProperty, ObjectProperty
from kivy.uix.screenmanager import Screen
from kivy.uix.label import Label
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.gridlayout import GridLayout
from functools import partial
from kivy.core.window import Window
from kivy.uix.button import Button
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
import locale


from collections import namedtuple
from kivy.uix.widget import Widget
from kivy.uix.anchorlayout import AnchorLayout
from kivy.graphics import Color, Line
from jnius import autoclass, PythonJavaClass, java_method, cast
from android.runnable import run_on_ui_thread
 
# preload java classes
System = autoclass('java.lang.System')
System.loadLibrary('iconv')
PythonActivity = autoclass('org.renpy.android.PythonActivity')
Camera = autoclass('android.hardware.Camera')
ImageScanner = autoclass('net.sourceforge.zbar.ImageScanner')
Config = autoclass('net.sourceforge.zbar.Config')
SurfaceView = autoclass('android.view.SurfaceView')
LayoutParams = autoclass('android.view.ViewGroup$LayoutParams')
Image = autoclass('net.sourceforge.zbar.Image')
ImageFormat = autoclass('android.graphics.ImageFormat')
LinearLayout = autoclass('android.widget.LinearLayout')
Symbol = autoclass('net.sourceforge.zbar.Symbol')
 
 
class PreviewCallback(PythonJavaClass):
    '''Interface used to get back the preview frame of the Android Camera
    '''
    __javainterfaces__ = ('android.hardware.Camera$PreviewCallback', )
 
    def __init__(self, callback):
        super(PreviewCallback, self).__init__()
        self.callback = callback
 
    @java_method('([BLandroid/hardware/Camera;)V')
    def onPreviewFrame(self, data, camera):
        self.callback(camera, data)
 
 
class SurfaceHolderCallback(PythonJavaClass):
    '''Interface used to know exactly when the Surface used for the Android
    Camera will be created and changed.
    '''
 
    __javainterfaces__ = ('android.view.SurfaceHolder$Callback', )
 
    def __init__(self, callback):
        super(SurfaceHolderCallback, self).__init__()
        self.callback = callback
  
    @java_method('(Landroid/view/SurfaceHolder;III)V')
    def surfaceChanged(self, surface, fmt, width, height):
        self.callback(fmt, width, height)
 
    @java_method('(Landroid/view/SurfaceHolder;)V')
    def surfaceCreated(self, surface):
        pass
  
    @java_method('(Landroid/view/SurfaceHolder;)V')
    def surfaceDestroyed(self, surface):
        pass
 
 
class AndroidWidgetHolder(Widget):
    '''Act as a placeholder for an Android widget.
    It will automatically add / remove the android view depending if the widget
    view is set or not. The android view will act as an overlay, so any graphics
    instruction in this area will be covered by the overlay.
    '''
 
    view = ObjectProperty(allownone=True)
    '''Must be an Android View
    '''
 
    def __init__(self, **kwargs):
        self._old_view = None
        from kivy.core.window import Window
        self._window = Window
        kwargs['size_hint'] = (None, None)
        super(AndroidWidgetHolder, self).__init__(**kwargs)
 
    def on_view(self, instance, view):
        if self._old_view is not None:
            layout = cast(LinearLayout, self._old_view.getParent())
            layout.removeView(self._old_view)
            self._old_view = None
 
        if view is None:
            return
 
        activity = PythonActivity.mActivity
        activity.addContentView(view, LayoutParams(*self.size))
        view.setZOrderOnTop(True)
        view.setX(self.x)
        view.setY(self._window.height - self.y - self.height)
        self._old_view = view
 
    def on_size(self, instance, size):
        if self.view:
            params = self.view.getLayoutParams()
            params.width = self.width
            params.height = self.height
            self.view.setLayoutParams(params)
            self.view.setY(self._window.height - self.y - self.height)
 
    def on_x(self, instance, x):
        if self.view:
            self.view.setX(x)
 
    def on_y(self, instance, y):
        if self.view:
            self.view.setY(self._window.height - self.y - self.height)
 
 
class AndroidCamera(Widget):
    '''Widget for controling an Android Camera.
    '''
 
    index = NumericProperty(0)
 
    __events__ = ('on_preview_frame', )
 
    def __init__(self, **kwargs):
        self._holder = None
        self._android_camera = None
        super(AndroidCamera, self).__init__(**kwargs)
        self._holder = AndroidWidgetHolder(size=self.size, pos=self.pos)
        self.add_widget(self._holder)
 
    @run_on_ui_thread
    def stop(self):
        if self._android_camera is None:
            return
        self._android_camera.setPreviewCallback(None)
        self._android_camera.release()
        self._android_camera = None
        self._holder.view = None
 
    @run_on_ui_thread
    def start(self):
        if self._android_camera is not None:
            return
 
        self._android_camera = Camera.open(self.index)
 
        # create a fake surfaceview to get the previewCallback working.
        self._android_surface = SurfaceView(PythonActivity.mActivity)
        surface_holder = self._android_surface.getHolder()
 
        # create our own surface holder to correctly call the next method when
        # the surface is ready
        self._android_surface_cb = SurfaceHolderCallback(self._on_surface_changed)
        surface_holder.addCallback(self._android_surface_cb)
 
        # attach the android surfaceview to our android widget holder
        self._holder.view = self._android_surface
 
    def _on_surface_changed(self, fmt, width, height):
        # internal, called when the android SurfaceView is ready
        # FIXME if the size is not handled by the camera, it will failed.
        params = self._android_camera.getParameters()
        params.setPreviewSize(width, height)
        self._android_camera.setParameters(params)
 
        # now that we know the camera size, we'll create 2 buffers for faster
        # result (using Callback buffer approach, as described in Camera android
        # documentation)
        # it also reduce the GC collection
        bpp = ImageFormat.getBitsPerPixel(params.getPreviewFormat()) / 8.
        buf = '\x00' * int(width * height * bpp)
        self._android_camera.addCallbackBuffer(buf)
        self._android_camera.addCallbackBuffer(buf)
 
        # create a PreviewCallback to get back the onPreviewFrame into python
        self._previewCallback = PreviewCallback(self._on_preview_frame)
 
        # connect everything and start the preview
        self._android_camera.setPreviewCallbackWithBuffer(self._previewCallback);
        self._android_camera.setPreviewDisplay(self._android_surface.getHolder())
        self._android_camera.startPreview();
 
    def _on_preview_frame(self, camera, data):
        # internal, called by the PreviewCallback when onPreviewFrame is
        # received
        self.dispatch('on_preview_frame', camera, data)
        # reintroduce the data buffer into the queue
        self._android_camera.addCallbackBuffer(data)
 
    def on_preview_frame(self, camera, data):
        pass
 
    def on_size(self, instance, size):
        if self._holder:
            self._holder.size = size
 
    def on_pos(self, instance, pos):
        if self._holder:
            self._holder.pos = pos
 
 
class ZbarQrcodeDetector(AnchorLayout):
    '''Widget that use the AndroidCamera and zbar to detect qrcode.
    When found, the `symbols` will be updated
    '''
    camera_size = ListProperty([320, 240])
 
    symbols = ListProperty([])
 
    # XXX can't work now, due to overlay.
    show_bounds = BooleanProperty(False)
 
    Qrcode = namedtuple('Qrcode',
            ['type', 'data', 'bounds', 'quality', 'count'])
 
    def __init__(self, **kwargs):
        super(ZbarQrcodeDetector, self).__init__(**kwargs)
        self._camera = AndroidCamera(
                size=self.camera_size,
                size_hint=(None, None))
        self._camera.bind(on_preview_frame=self._detect_qrcode_frame)
        self.add_widget(self._camera)
 
        # create a scanner used for detecting qrcode
        self._scanner = ImageScanner()
        self._scanner.setConfig(0, Config.ENABLE, 0)
        self._scanner.setConfig(Symbol.QRCODE, Config.ENABLE, 1)
        self._scanner.setConfig(0, Config.X_DENSITY, 3)
        self._scanner.setConfig(0, Config.Y_DENSITY, 3)
 
    def start(self):
        self._camera.start()
 
    def stop(self):
        self._camera.stop()
 
    def _detect_qrcode_frame(self, instance, camera, data):
        # the image we got by default from a camera is using the NV21 format
        # zbar only allow Y800/GREY image, so we first need to convert,
        # then start the detection on the image
        parameters = camera.getParameters()
        size = parameters.getPreviewSize()
        barcode = Image(size.width, size.height, 'NV21')
        barcode.setData(data)
        barcode = barcode.convert('Y800')
 
        result = self._scanner.scanImage(barcode)
 
        if result == 0:
            self.symbols = []
            return
 
        # we detected qrcode! extract and dispatch them
        symbols = []
        it = barcode.getSymbols().iterator()
        while it.hasNext():
            symbol = it.next()
            qrcode = ZbarQrcodeDetector.Qrcode(
                type=symbol.getType(),
                data=symbol.getData(),
                quality=symbol.getQuality(),
                count=symbol.getCount(),
                bounds=symbol.getBounds())
            symbols.append(qrcode)
 
        self.symbols = symbols
 
    '''
    # can't work, due to the overlay.
    def on_symbols(self, instance, value):
        if self.show_bounds:
            self.update_bounds()
 
    def update_bounds(self):
        self.canvas.after.remove_group('bounds')
        if not self.symbols:
            return
        with self.canvas.after:
            Color(1, 0, 0, group='bounds')
            for symbol in self.symbols:
                x, y, w, h = symbol.bounds
                x = self._camera.right - x - w
                y = self._camera.top - y - h
                Line(rectangle=[x, y, w, h], group='bounds')
    '''
 





db_filename = 'trazabilidad.db'

db_is_new = not os.path.exists(db_filename)

conn = sqlite3.connect(db_filename)

cursor = conn.cursor()

if db_is_new:
    cursor.execute("""CREATE TABLE products (name text, barcode text, cant real, type text)""")
    cursor.execute("""CREATE TABLE connect (url text, db text, user text, pwd text, id integer)""")
    cursor.execute("""INSERT INTO connect (url, db, user, pwd, id) values ('direccion del servidor', 'base de datos', 'usuario', 'password', 1)""")
    conn.commit()

Builder.load_string('''
# define how clabel looks and behaves
<CLabel>:
  canvas.before:
    Color:
      rgb: self.bgcolor
    Rectangle:
      size: self.size
      pos: self.pos

<HeaderLabel>:
  canvas.before:
    Color:
      rgb: self.bgcolor
    Rectangle:
      size: self.size
      pos: self.pos
'''
)

class CLabel(ToggleButton):
    bgcolor = ListProperty([1,1,1,1])

class HeaderLabel(Label):
    bgcolor = ListProperty([0.611,0.411,0.276,0.276])
    
counter = 0
class DataGrid(GridLayout):
    def add_row(self, row_data, row_align, cols_size, **kwargs):
        global counter

        ##########################################################
        def change_on_press(self):
            childs = self.parent.children
            for ch in childs:
                if ch.id == self.id:
#                     print ch.id
#                     print len(ch.id)
                    row_n = 0
                    if len(ch.id) == 11:
                        row_n = ch.id[4:5]
                    else:
                        row_n = ch.id[4:6]
                    for c in childs:
                        if ('row_'+str(row_n)+'_col_0') == c.id:
                            if c.state == "normal":
                                c.state="down"
                            else:    
                                c.state="normal"
                        if ('row_'+str(row_n)+'_col_1') == c.id:
                            if c.state == "normal":
                                c.state="down"
                            else:    
                                c.state="normal"
                        if ('row_'+str(row_n)+'_col_2') == c.id:
                            if c.state == "normal":
                                c.state="down"
                            else:    
                                c.state="normal"
                        if ('row_'+str(row_n)+'_col_3') == c.id:
                            if c.state == "normal":
                                c.state="down"
                            else:
                                c.state="normal"
        def change_on_release(self):
            if self.state == "normal":
                self.state = "down"
            else:
                self.state = "normal"
        ##########################################################
        
        n = 0
        for item in row_data:
            cell = CLabel(text=('[color=000000]' + str(item) + '[/color]'), 
                                        background_normal="background_normal.png",
                                        background_down="background_pressed.png",
                                        halign=row_align[n],
                                        markup=True,
                                        on_press=partial(change_on_press),
                                        on_release=partial(change_on_release),
                                        text_size=(0, None),
                                        size_hint_x=cols_size[n], 
                                        size_hint_y=None,
                                        height=40,
                                        id=("row_" + str(counter) + "_col_" + str(n)))
            cell_width = Window.size[0] * cell.size_hint_x
            cell.text_size=(cell_width - 30, None)
            cell.texture_update()
            self.add_widget(cell)
            n+=1
        counter += 1
        
        
    def insert(self, txt_producto, txt_cantidad, txt_codigo, ty):
        try:
            cant = float(txt_cantidad)
            error = False
        except ValueError:
            content = Button(text='ATENCIÓN: Debe ingresar un número como cantidad!')
            popup = Popup(title='Error', content=content, auto_dismiss=False, size_hint=(None, None), size=(400, 400))
            content.bind(on_press=popup.dismiss)
            popup.open()
            error = True
        if not error:
            if ty=="in":
                self.add_row([txt_producto, txt_codigo, cant, "in"], ["center", "center", "center", "center"], [0.5, 0.2, 0.2, 0.2])
                t = (txt_producto, txt_codigo, cant, "in")
            elif ty=="out":
                self.add_row([txt_producto, txt_codigo, cant, "out"], ["center", "center", "center", "center"], [0.5, 0.2, 0.2, 0.2])
                t = (txt_producto, txt_codigo, cant, "out")
            cursor.execute("""INSERT INTO products values (?,?,?,?)""", t)
            conn.commit()
        
    def remove_row(self, **kwargs):
        rem_row = ()
        n_cols = 4
        childs = self.parent.children
        selected = 0
        for ch in childs:
            for c in reversed(ch.children):
                if c.id != "Header_Label":
                    if c.state == "down":
                        self.remove_widget(c)
#                         print str(c.id) + '   -   ' + str(c.state)
                        selected += 1
                        column = c.text.replace("[color=000000]", "")
                        column = column.replace("[/color]", "")
#                         print column
                        rem_row = rem_row + (column,)
        if selected == 0:
            for ch in childs:
#                 count_01 = n_cols
#                 count_02 = 0
                count = 0
                while (count < n_cols):
                    if n_cols != len(ch.children):
                        for c in ch.children:
                            if c.id != "Header_Label":
#                                 print "Length: " + str(len(ch.children))
#                                 print "N_cols: " + str(n_cols + 1)
                         
                                self.remove_widget(c)
                                count += 1
                                break
                            else:
                                break
                    else:
                        break
        else:
#             print rem_row
            cursor.execute('DELETE FROM products WHERE name=? and barcode=? and cant=? and type=?', rem_row)
            conn.commit()
            
    def export(self, **kwargs):
#         host= '192.168.1.53'
        protocol= 'xmlrpc'
        port=8069
#         dbname = 'v6scan'
#         username = 'admin'
#         pwd = 'admin'
        
        cursor.execute("""SELECT * FROM connect""")
        connection_data = cursor.fetchone()
         
        host = connection_data[0]
        dbname = connection_data[1]
        username = connection_data[2]
        pwd = connection_data[3]
        
        try:  
            oerp = oerplib.OERP(host, dbname, protocol, port)
            user = oerp.login(username, pwd)
        except:
#             print "no conecto"
            return False
#             content = BoxLayout(Label(text='Conexión a Servidor Incorrecta'), Button(text='Volver', on_release=self._popup.dismiss()))
#             self._popup = Popup(title="load file", content=content, \
#                 size_hint=(0.9, 0.9))
#             self._popup.open()
        #read sqlite database to load products
        cursor.execute("""SELECT * FROM products""")
        sel = cursor.fetchall()
        for product_sqlite in sel:
            #search the product in openerp
            product_args = [('ean13', '=', product_sqlite[1])]
            product_ids = oerp.execute('product.product', 'search', product_args)
            product_id = product_ids[0]
            product_name = oerp.execute('product.product', 'read', product_id, ['name_template'])
            #create stock_move
            move = {
                'product_id': product_id,
                'product_qty': product_sqlite[2],
                'product_uom': 1,
                'location_id': 8,
                'location_dest_id': 12,
                'name': product_name['name_template'],
            }
            move_id = oerp.execute('stock.move', 'create', move)
#             print "new stock_move: %s"%move_id
            #delete all the incoming moves in sqlite
            cursor.execute('DELETE FROM products WHERE type=?', ("in",))
            conn.commit()
            #delete all the rows in the view
            childs = self.parent.children
            for ch in childs:
                for c in reversed(ch.children):
                    if c.id != "Header_Label":
                        self.remove_widget(c)
                        
                        
    def export_out(self, **kwargs):
        # host= 'localhost'
        protocol= 'xmlrpc'
        port=8069
        # dbname = 'v6scan'
        # username = 'admin'
        # pwd = 'admin'
        
        cursor.execute("""SELECT * FROM connect""")
        connection_data = cursor.fetchone()
        
        host = connection_data[0]
        dbname = connection_data[1]
        username = connection_data[2]
        pwd = connection_data[3]
        
        try:  
            oerp = oerplib.OERP(host, dbname, protocol, port)
            user = oerp.login(username, pwd)
        except:
            return False
        
        #read sqlite database to load products
        cursor.execute("""SELECT * FROM products""")
        sel = cursor.fetchall()
        for product_sqlite in sel:
            #search the product in openerp
            product_args = [('ean13', '=', product_sqlite[1])]
            product_ids = oerp.execute('product.product', 'search', product_args)
            product_id = product_ids[0]
            product_name = oerp.execute('product.product', 'read', product_id, ['name_template'])
            #create stock_move
            move = {
                'product_id': product_id,
                'product_qty': product_sqlite[2],
                'product_uom': 1,
                'location_id': 12,
                'location_dest_id': 9,
                'name': product_name['name_template'],
            }
            move_id = oerp.execute('stock.move', 'create', move)
#             print "new stock_move: %s"%move_id
            #delete all the incoming moves in sqlite
            cursor.execute('DELETE FROM products WHERE type=?', ("out",))
            conn.commit()
            #delete all the rows in the view
            childs = self.parent.children
            for ch in childs:
                for c in reversed(ch.children):
                    if c.id != "Header_Label":
                        self.remove_widget(c)        
                        
#     def scan(self, **kwargs):
#         droid = android.Android()
#         code = droid.scanBarcode()
#         self.txt_codigo.text = code
                        
        
    def __init__(self, **kwargs):
        header_data = ['Producto', 'Código', 'Cantidad', 'Tipo']
        cols_size = [0.5, 0.2, 0.2, 0.2]
        super(DataGrid, self).__init__(**kwargs)
        self.size_hint_y=None
        self.bind(minimum_height=self.setter('height'))
        self.cols = len(header_data)
        self.spacing = [2]
        n = 0
        for hcell in header_data:
            header_str = "[b]" + str(hcell) + "[/b]"
            self.add_widget(HeaderLabel(text=header_str, 
                markup=True, 
                size_hint_y=None,
                height=40,
                id="Header_Label",
                size_hint_x=cols_size[n]))
            n+=1
        #read sqlite database to load products
        cursor.execute("""SELECT * FROM products""")
        sel = cursor.fetchall()
#         print sel
#         print len(sel)
        self.rows = len(sel) + 1
        for product in sel:
#             print product
#             print product[0]
#             print product[1]
#             print product[2]
            self.add_row([product[0], product[1], product[2], product[3]], ["center", "center", "center", "center"], [0.5, 0.2, 0.2, 0.2])


class TrazabilidadScreen(Screen):
    fullscreen = BooleanProperty(False)
    
    cursor.execute("""SELECT * FROM connect""")
    connection_data = cursor.fetchone()
    
    url = connection_data[0]
    db = connection_data[1]
    user = connection_data[2]
    pwd = connection_data[3]

    def add_widget(self, *args):
        if 'content' in self.ids:
            return self.ids.content.add_widget(*args)
        return super(TrazabilidadScreen, self).add_widget(*args)
    

class TrazabilidadApp(App):
    
    index = NumericProperty(-1)
    current_title = StringProperty()
    screen_names = ListProperty([])
    higherarchy = ListProperty([])
    
    def change_url(self, text):
        self.url = text
        cursor.execute("""UPDATE connect SET url = ? WHERE id=1""", [self.url])
        conn.commit()
    
    def change_bd(self, text):
        self.db = text
        cursor.execute("""UPDATE connect SET db = ? WHERE id=1""", [self.db])
        conn.commit()
        
    def change_user(self, text):
        self.user = text
        cursor.execute("""UPDATE connect SET user = ? WHERE id=1""", [self.user])
        conn.commit()
        
    def change_pwd(self, text):
        self.pwd = text
        cursor.execute("""UPDATE connect SET pwd = ? WHERE id=1""", [self.pwd])
        conn.commit()

    def new_code_in(self, text):
#         android = android.Android()
#         self.code = android.scanBarcode()
        self.code = text
        cursor.execute("""INSERT INTO products (name, barcode, cant, type) values ('product', ?, 'none', 'in')""", [self.code])
        conn.commit()
        
    def new_code_out(self, text):
        self.code = text
        cursor.execute("""INSERT INTO products (name, barcode, cant, type) values ('product', ?, 'none', 'out')""", [self.code])
        conn.commit()
        
    def guardar_producto(self, codigo_in, prod_in, cant_in):
#         print codigo_in
        pass
    
    def build(self):
        self.title = 'Entrada y salida de productos'
        self.screens = {}
        self.available_screens = [
            'Inicio','Entradas', 'Salidas', 'Configurar']
        self.screen_names = self.available_screens
        curdir = dirname(__file__)
        self.available_screens = [join(curdir, 'data', 'screens',
            '{}.kv'.format(fn)) for fn in self.available_screens]
        self.go_next_screen()
        
    def on_pause(self):
        return True

    def on_resume(self):
        pass

    def on_current_title(self, instance, value):
        self.root.ids.spnr.text = value

    def go_previous_screen(self):
        self.index = (self.index - 1) % len(self.available_screens)
        screen = self.load_screen(self.index)
        sm = self.root.ids.sm
        sm.switch_to(screen, direction='right')
        self.current_title = screen.name

    def go_next_screen(self):
        self.index = (self.index + 1) % len(self.available_screens)
        screen = self.load_screen(self.index)
        sm = self.root.ids.sm
        sm.switch_to(screen, direction='left')
        self.current_title = screen.name

    def go_screen(self, idx):
        self.index = idx
        self.root.ids.sm.switch_to(self.load_screen(idx), direction='left')

    def go_higherarchy_previous(self):
        ahr = self.higherarchy
        if len(ahr) == 1:
            return
        if ahr:
            ahr.pop()
        if ahr:
            idx = ahr.pop()
            self.go_screen(idx)

    def load_screen(self, index):
        if index in self.screens:
            return self.screens[index]
        screen = Builder.load_file(self.available_screens[index].lower())
        self.screens[index] = screen
        return screen

if __name__ == "__main__":
    TrazabilidadApp().run()