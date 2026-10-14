"""Small native drawing primitives for Loqalyn's light workspace theme.

Controls remain NSButtons with native focus, keyboard actions and accessibility.
No embedded browser or custom input event loop is involved.
"""
import AppKit as C
import Foundation as F
import objc


def color(value, alpha=1):
    value=value.lstrip('#')
    return C.NSColor.colorWithSRGBRed_green_blue_alpha_(int(value[:2],16)/255,int(value[2:4],16)/255,int(value[4:],16)/255,alpha)

INK=color('172D32')
MUTED=color('6B7C83')
ACCENT=color('087F72')
CANVAS=color('F5F7F8')
SURFACE=color('FFFFFF')
LINE=color('E1E7E9')
SIDEBAR=color('172B30')
NAV_TEXT=color('BAC9CC')


class FlatButton(C.NSButton):
    def drawRect_(self, dirty):
        bounds=self.bounds(); enabled=self.isEnabled(); tone=getattr(self,'tone','secondary')
        selected=tone=='nav' and bool(self.state())
        if tone=='nav':
            background=color('284B4A') if selected else SIDEBAR
            foreground=color('B6F0D9') if selected else NAV_TEXT
        elif tone=='dark':
            background=SIDEBAR; foreground=NAV_TEXT
        elif not enabled:
            background=color('EBEFF0'); foreground=color('9BA8AE')
        elif tone=='primary':
            background=ACCENT; foreground=SURFACE
        else:
            background=SURFACE; foreground=INK
        if enabled and self.cell().isHighlighted(): background=background.blendedColorWithFraction_ofColor_(.12,INK)
        shape=C.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(F.NSInsetRect(bounds,1,1),8,8)
        background.setFill(); shape.fill()
        if tone=='secondary' and enabled: LINE.setStroke(); shape.setLineWidth_(1); shape.stroke()
        if enabled and self.window() and self.window().firstResponder()==self:
            ACCENT.setStroke(); shape.setLineWidth_(2); shape.stroke()
        text=F.NSAttributedString.alloc().initWithString_attributes_(self.title(),{
            C.NSFontAttributeName:C.NSFont.systemFontOfSize_weight_(13,C.NSFontWeightMedium),
            C.NSForegroundColorAttributeName:foreground})
        size=text.size(); symbol=getattr(self,'symbol',None); icon=None
        if symbol:
            icon=C.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol,None)
            if icon: icon=icon.imageWithSymbolConfiguration_(C.NSImageSymbolConfiguration.configurationWithPaletteColors_([foreground]))
        left=14 if tone in ('nav','dark') else max(12,(bounds.size.width-size.width-(25 if icon else 0))/2)
        if icon:
            icon.drawInRect_fromRect_operation_fraction_(F.NSMakeRect(left,(bounds.size.height-16)/2,16,16),F.NSZeroRect,C.NSCompositingOperationSourceOver,1 if enabled else .5)
            left+=26
        text.drawAtPoint_(F.NSMakePoint(left,(bounds.size.height-size.height)/2))


class MemoryMeter(C.NSView):
    @objc.python_method
    def setDoubleValue_(self,value):
        self.value=max(0,min(100,value)); self.setNeedsDisplay_(True)

    def drawRect_(self, dirty):
        bounds=self.bounds()
        track=C.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds,4,4)
        LINE.setFill(); track.fill()
        width=bounds.size.width*getattr(self,'value',0)/100
        if width>0:
            fill=C.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(F.NSMakeRect(0,0,width,bounds.size.height),4,4)
            ACCENT.setFill(); fill.fill()
