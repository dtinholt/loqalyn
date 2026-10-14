"""Loqalyn's native Cocoa interface. No browser, HTML, or WebView is used.

All controller operations run on daemon workers. Only callbacks marshalled by
AppHelper.callAfter touch AppKit. Validation and approval invariants live in
desktop_presenter.py so they can be tested without a macOS window server.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import AppKit as C
import Foundation as F
import objc
import yaml
from PyObjCTools import AppHelper
from .desktop_style import FlatButton, MemoryMeter, color, INK, MUTED, ACCENT, CANVAS, SURFACE, LINE, SIDEBAR as SIDEBAR_COLOR, NAV_TEXT

from .desktop_presenter import (
    ApprovalReview, PAGES, ROUTE_NAMES, RUNTIME_NAMES, StreamBuffer,
    approval_input, decision_text, filtered_models, format_time, load_blocker,
    model_patch, model_status, model_summary, policy_yaml, pretty, request_payload,
    settings_policy, unload_blocker,
)

WIDTH = 1200
HEIGHT = 820
SIDEBAR = 208
PAGE_WIDTH = WIDTH - SIDEBAR - 40


def rect(x, y, width, height):
    return F.NSMakeRect(x, y, width, height)


class FlippedView(C.NSView):
    def isFlipped(self):
        return True

    def resizeSubviewsWithOldSize_(self, old_size):
        objc.super(FlippedView, self).resizeSubviewsWithOldSize_(old_size)
        callback = getattr(self, "layout_callback", None)
        if callback:
            callback()


class TableSource(F.NSObject, protocols=[objc.protocolNamed("NSTableViewDataSource"), objc.protocolNamed("NSTableViewDelegate")]):
    @objc.python_method
    def configure(self, columns, selection):
        self.rows = []
        self.columns = columns
        self.selection_callback = selection
        self.suppress_selection = False
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row < 0 or row >= len(self.rows):
            return ""
        formatter = self.columns.get(str(column.identifier()))
        return str(formatter(self.rows[row])) if formatter else ""

    def tableViewSelectionDidChange_(self, notification):
        if not self.suppress_selection:
            self.selection_callback()


class DesktopApplication(F.NSObject, protocols=[objc.protocolNamed("NSApplicationDelegate"), objc.protocolNamed("NSWindowDelegate")]):
    @objc.python_method
    def configure(self, controller):
        self.controller = controller
        self.state = None
        self.started = False
        self.starting = False
        self.quitting = False
        self.stopped = False
        self.actions = {}
        self.next_action = 1
        self.pending = set()
        self.deferred_callbacks = []
        self.pages = {}
        self.nav_buttons = {}
        self.active_page = "Overview"
        self.controls = {}
        self.ui_ready = False
        self.stream_cancel = None
        self.stream_buffer = StreamBuffer()
        self.stream_lock = threading.Lock()
        self.stream_render_pending = False
        self.review = None
        self.review_record = None
        self.review_request_id = None
        self.job_ids = set()
        self.download_states = {}
        self.hub_preview = None
        self.last_stream_render = 0
        self.last_stream_text = None
        self.policy_loaded = False
        self.settings_loaded = False
        self.last_connection_error = None
        self.claude_client = {'compatible':False,'message':'Checking Claude Code…'}
        self.claude_project = None
        return self

    def applicationDidFinishLaunching_(self, notification):
        self._build_menu()
        self._build_window()
        self._build_status_item()
        self.timer = F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "poll:", None, True)
        self.showWindow_(None)
        self._start()

    @objc.typedSelector(b"B@:@")
    def applicationShouldTerminateAfterLastWindowClosed_(self, application):
        return False

    # AppKit's protocol uses C99 bool on arm64. PyObjC's legacy selector
    # metadata infers NSBOOL here, which fails protocol validation at import.
    @objc.typedSelector(b"B@:@B")
    def applicationShouldHandleReopen_hasVisibleWindows_(self, application, has_windows):
        self.showWindow_(None)
        return True

    @objc.typedSelector(b"B@:@")
    def windowShouldClose_(self, window):
        window.orderOut_(None)
        return False

    @objc.typedSelector(b"Q@:@")
    def applicationShouldTerminate_(self, application):
        if self.stopped:
            return C.NSTerminateNow
        if self.quitting:
            return C.NSTerminateLater
        self.quitting = True
        self.timer.invalidate()
        self._sync_buttons()
        if self.started:
            self._status("Checking active requests before quitting…")
            self._work("quit-check", lambda: self.controller.call("/loqalyn/status"), self._quit_checked, quiet=True, allow_quit=True)
        else:
            self._begin_shutdown()
        return C.NSTerminateLater

    @objc.python_method
    def _quit_checked(self, state):
        active = int(state.get("active", 0))
        if active or self.stream_cancel is not None or self.job_ids:
            if not self._confirm("Quit Loqalyn?", f"{max(active, 1)} request(s) or downloads may be interrupted. The local endpoint will stop. Model runtimes remain separate applications.", "Quit", destructive=True):
                self._cancel_quit()
                return
        self._begin_shutdown()

    @objc.python_method
    def _cancel_quit(self):
        self.quitting = False
        self.timer = F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "poll:", None, True)
        C.NSApplication.sharedApplication().replyToApplicationShouldTerminate_(False)
        self._sync_buttons()
        self._status("Loqalyn is still running.")
        deferred, self.deferred_callbacks = self.deferred_callbacks, []
        for callback in deferred:
            AppHelper.callAfter(self._work_done, *callback)
        self._render_stream()

    @objc.python_method
    def _begin_shutdown(self):
        if self.stream_cancel:
            self.stream_cancel.set()
        self._status("Stopping the local service…")
        self._work("shutdown", self.controller.stop, self._shutdown_complete, quiet=True, allow_quit=True)

    @objc.python_method
    def _shutdown_complete(self, result):
        self.stopped = True
        C.NSApplication.sharedApplication().replyToApplicationShouldTerminate_(True)

    @objc.IBAction
    def showWindow_(self, sender):
        self.window.makeKeyAndOrderFront_(None)
        C.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.IBAction
    def handleAction_(self, sender):
        callback = self.actions.get(int(sender.tag()))
        if callback:
            try:
                callback()
            except Exception as exc:
                self._error(str(exc))

    @objc.IBAction
    def filterModels_(self, sender):
        self._reload_models()

    def poll_(self, timer):
        if self.started and not self.quitting:
            self._refresh(False, quiet=True)
            for job_id in tuple(self.job_ids):
                self._poll_job(job_id)

    @objc.python_method
    def _action(self, control, callback):
        tag = self.next_action
        self.next_action += 1
        self.actions[tag] = callback
        control.setTag_(tag)
        control.setTarget_(self)
        control.setAction_("handleAction:")
        return control

    @objc.python_method
    def _button(self, parent, title, x, y, width, callback, key=None):
        button = FlatButton.alloc().initWithFrame_(rect(x, y, width, 34))
        button.setTitle_(title)
        button.setBordered_(False)
        button.tone = "primary" if key in ("generate", "save_policy", "save_settings", "preview", "approve", "load", "hub_install") or title == "Open playground" else "secondary"
        button.setFont_(C.NSFont.systemFontOfSize_(13))
        self._action(button, callback)
        if key:
            self.controls[key] = button
        parent.addSubview_(button)
        return button

    @objc.python_method
    def _label(self, parent, text, x, y, width, height=22, size=13, secondary=False, bold=False, stretch=False):
        field = C.NSTextField.alloc().initWithFrame_(rect(x, y, width, height))
        field.setEditable_(False)
        field.setSelectable_(True)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setStringValue_(text)
        field.setFont_(C.NSFont.systemFontOfSize_weight_(size, C.NSFontWeightSemibold if bold else C.NSFontWeightRegular))
        field.setTextColor_(MUTED if secondary else INK)
        field.cell().setWraps_(True)
        field.cell().setScrollable_(False)
        if stretch:
            field.setAutoresizingMask_(C.NSViewWidthSizable)
        parent.addSubview_(field)
        return field

    @objc.python_method
    def _field(self, parent, label, value, x, y, width, *, secure=False):
        self._label(parent, label, x, y, width, secondary=True, size=11)
        cls = C.NSSecureTextField if secure else C.NSTextField
        field = cls.alloc().initWithFrame_(rect(x, y + 23, width, 31))
        field.setStringValue_(str(value))
        field.setFont_(C.NSFont.systemFontOfSize_(13))
        field.setBezeled_(False)
        field.setDrawsBackground_(True)
        field.setBackgroundColor_(SURFACE)
        field.setWantsLayer_(True)
        field.layer().setCornerRadius_(6)
        field.layer().setBorderWidth_(1)
        field.layer().setBorderColor_(LINE.CGColor())
        parent.addSubview_(field)
        return field

    @objc.python_method
    def _check(self, parent, title, x, y, width, value=False, callback=None):
        button = C.NSButton.alloc().initWithFrame_(rect(x, y, width, 28))
        button.setButtonType_(C.NSButtonTypeSwitch)
        button.setTitle_(title)
        button.setFont_(C.NSFont.systemFontOfSize_(13))
        button.setState_(C.NSControlStateValueOn if value else C.NSControlStateValueOff)
        if callback:
            self._action(button, callback)
        parent.addSubview_(button)
        return button

    @objc.python_method
    def _popup(self, parent, items, x, y, width):
        popup = C.NSPopUpButton.alloc().initWithFrame_pullsDown_(rect(x, y, width, 29), False)
        for title, value in items:
            popup.addItemWithTitle_(title)
            popup.lastItem().setRepresentedObject_(value)
        parent.addSubview_(popup)
        return popup

    @objc.python_method
    def _popup_value(self, popup):
        item = popup.selectedItem()
        return str(item.representedObject()) if item else ""

    @objc.python_method
    def _select_popup(self, popup, value):
        for index, item in enumerate(popup.itemArray()):
            if str(item.representedObject()) == str(value):
                popup.selectItemAtIndex_(index)
                return

    @objc.python_method
    def _editor(self, parent, x, y, width, height, *, editable=True, mono=False):
        scroll = C.NSScrollView.alloc().initWithFrame_(rect(x, y, width, height))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(C.NSNoBorder)
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(10)
        scroll.layer().setBorderWidth_(1)
        scroll.layer().setBorderColor_(LINE.CGColor())
        scroll.layer().setMasksToBounds_(True)
        scroll.setAutoresizingMask_(C.NSViewWidthSizable)
        text = C.NSTextView.alloc().initWithFrame_(rect(0, 0, width - 18, height))
        text.setRichText_(False)
        text.setEditable_(editable)
        text.setSelectable_(True)
        text.setAllowsUndo_(editable)
        text.setAutomaticQuoteSubstitutionEnabled_(False)
        text.setAutomaticDashSubstitutionEnabled_(False)
        text.setAutomaticTextReplacementEnabled_(False)
        text.setFont_(C.NSFont.monospacedSystemFontOfSize_weight_(12, C.NSFontWeightRegular) if mono else C.NSFont.systemFontOfSize_(14))
        text.setBackgroundColor_(SURFACE)
        text.setTextColor_(INK)
        text.setTextContainerInset_(F.NSMakeSize(14, 14))
        text.setMinSize_(F.NSMakeSize(0, height))
        text.setMaxSize_(F.NSMakeSize(1000000, 1000000))
        text.setVerticallyResizable_(True)
        text.setHorizontallyResizable_(False)
        text.setAutoresizingMask_(C.NSViewWidthSizable)
        text.textContainer().setContainerSize_(F.NSMakeSize(width - 18, 1000000))
        text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text)
        parent.addSubview_(scroll)
        return text

    @objc.python_method
    def _table(self, parent, columns, y, height, callback):
        scroll = C.NSScrollView.alloc().initWithFrame_(rect(24, y, PAGE_WIDTH - 48, height))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(C.NSNoBorder)
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(10)
        scroll.layer().setBorderWidth_(1)
        scroll.layer().setBorderColor_(LINE.CGColor())
        scroll.layer().setMasksToBounds_(True)
        scroll.setAutoresizingMask_(C.NSViewWidthSizable)
        table = C.NSTableView.alloc().initWithFrame_(scroll.bounds())
        table.setUsesAlternatingRowBackgroundColors_(False)
        table.setBackgroundColor_(SURFACE)
        table.setIntercellSpacing_(F.NSMakeSize(12, 8))
        table.setAllowsMultipleSelection_(False)
        table.setRowHeight_(36)
        table.setColumnAutoresizingStyle_(C.NSTableViewLastColumnOnlyAutoresizingStyle)
        formatters = {}
        for identifier, title, width, formatter in columns:
            column = C.NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.headerCell().setStringValue_(title)
            column.headerCell().setFont_(C.NSFont.systemFontOfSize_weight_(11, C.NSFontWeightMedium))
            column.dataCell().setFont_(C.NSFont.systemFontOfSize_(13))
            column.dataCell().setTextColor_(INK)
            column.setWidth_(width)
            column.setMinWidth_(70)
            column.setEditable_(False)
            table.addTableColumn_(column)
            formatters[identifier] = formatter
        source = TableSource.alloc().init().configure(formatters, callback)
        table.setDataSource_(source)
        table.setDelegate_(source)
        scroll.setDocumentView_(table)
        parent.addSubview_(scroll)
        return table, source

    @objc.python_method
    def _build_menu(self):
        main = C.NSMenu.alloc().init()
        app_item = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Loqalyn", None, "")
        app_menu = C.NSMenu.alloc().initWithTitle_("Loqalyn")
        about = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("About Loqalyn", "orderFrontStandardAboutPanel:", "")
        app_menu.addItem_(about)
        app_menu.addItem_(C.NSMenuItem.separatorItem())
        settings = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Settings…", None, ",")
        self._action(settings, lambda: self._navigate("Settings"))
        app_menu.addItem_(settings)
        app_menu.addItem_(C.NSMenuItem.separatorItem())
        app_menu.addItem_(C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide Loqalyn", "hide:", "h"))
        app_menu.addItem_(C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Loqalyn", "terminate:", "q"))
        app_item.setSubmenu_(app_menu)
        main.addItem_(app_item)
        edit_item = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        edit = C.NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
            edit.addItem_(C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key))
        edit_item.setSubmenu_(edit)
        main.addItem_(edit_item)
        window_item = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Window", None, "")
        window_menu = C.NSMenu.alloc().initWithTitle_("Window")
        show = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show Loqalyn", "showWindow:", "0")
        show.setTarget_(self)
        window_menu.addItem_(show)
        window_menu.addItem_(C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Close Window", "performClose:", "w"))
        window_item.setSubmenu_(window_menu)
        main.addItem_(window_item)
        C.NSApplication.sharedApplication().setMainMenu_(main)

    @objc.python_method
    def _build_window(self):
        style = C.NSWindowStyleMaskTitled | C.NSWindowStyleMaskClosable | C.NSWindowStyleMaskMiniaturizable | C.NSWindowStyleMaskResizable
        self.window = C.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(rect(0, 0, WIDTH, HEIGHT), style, C.NSBackingStoreBuffered, False)
        self.window.setTitle_("Loqalyn")
        self.window.setMinSize_(F.NSMakeSize(1160, 760))
        self.window.setAppearance_(C.NSAppearance.appearanceNamed_(C.NSAppearanceNameAqua))
        self.window.setBackgroundColor_(CANVAS)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(C.NSWindowTitleHidden)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.window.center()
        self.root = FlippedView.alloc().initWithFrame_(rect(0, 0, WIDTH, HEIGHT))
        self.window.setContentView_(self.root)
        self.sidebar = FlippedView.alloc().initWithFrame_(rect(0,0,SIDEBAR,HEIGHT))
        self.sidebar.setWantsLayer_(True); self.sidebar.layer().setBackgroundColor_(SIDEBAR_COLOR.CGColor())
        self.root.addSubview_(self.sidebar)
        self._label(self.sidebar, "⇄", 23, 30, 28, 34, size=27, bold=True).setTextColor_(color('8DE4CB'))
        self._label(self.sidebar, "Loqalyn", 57, 34, 140, 28, size=19, bold=True).setTextColor_(SURFACE)
        self._label(self.sidebar, "LOCAL MODEL ROUTER", 25, 72, 175, 18, size=9).setTextColor_(NAV_TEXT)
        self._label(self.sidebar, "WORKSPACE", 25, 114, 165, 18, size=10).setTextColor_(color('769399'))
        symbols=("square.grid.2x2", "square.stack.3d.up", "magnifyingglass", "sparkles", "terminal", "slider.horizontal.3", "clock.arrow.circlepath", "checkmark.shield", "gearshape")
        for index, name in enumerate(PAGES):
            button = self._button(self.sidebar, name, 14, 141 + index * 44, SIDEBAR - 28, lambda page=name: self._navigate(page))
            button.setFrameSize_(F.NSMakeSize(SIDEBAR-28,38))
            button.setButtonType_(C.NSButtonTypePushOnPushOff)
            button.tone='nav'; button.symbol=symbols[index]
            button.setAlignment_(C.NSTextAlignmentLeft)
            self.nav_buttons[name] = button
        self.sidebar_health = self._label(self.sidebar, "Starting service…", 25, HEIGHT-192, SIDEBAR-46, 90, size=11)
        self.sidebar_health.setTextColor_(NAV_TEXT)
        self.sidebar_copy=self._button(self.sidebar, "Copy endpoint", 14, HEIGHT-95, SIDEBAR-28, self._copy_endpoint, "copy_endpoint")
        self.sidebar_copy.tone='dark'; self.sidebar_copy.symbol='link'
        self.sidebar_folder=self._button(self.sidebar, "Data folder", 14, HEIGHT-53, SIDEBAR-28, self._show_data_folder)
        self.sidebar_folder.tone='dark'; self.sidebar_folder.symbol='folder'
        self.main_scroll = C.NSScrollView.alloc().initWithFrame_(rect(SIDEBAR + 8, 10, PAGE_WIDTH, HEIGHT - 50))
        self.main_scroll.setHasVerticalScroller_(True)
        self.main_scroll.setAutohidesScrollers_(True)
        self.main_scroll.setDrawsBackground_(False)
        self.root.addSubview_(self.main_scroll)
        self.status_label = self._label(self.root, "Starting Loqalyn…", SIDEBAR + 24, HEIGHT - 32, PAGE_WIDTH - 30, 24, secondary=True, size=11)
        self._build_overview()
        self._build_models()
        self._build_discover()
        self._build_playground()
        self._build_claude()
        self._build_policies()
        self._build_activity()
        self._build_escalations()
        self._build_settings()
        self.root.layout_callback = self._layout
        self._layout()
        self._navigate("Overview", show=False)
        self.ui_ready = True
        self._sync_buttons()

    @objc.python_method
    def _build_status_item(self):
        self.status_item = C.NSStatusBar.systemStatusBar().statusItemWithLength_(C.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("⇄")
        self.status_item.button().setToolTip_("Loqalyn • Local model router")
        menu = C.NSMenu.alloc().init()
        self.status_menu_line = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Starting Loqalyn…", None, "")
        self.status_menu_line.setEnabled_(False)
        menu.addItem_(self.status_menu_line)
        menu.addItem_(C.NSMenuItem.separatorItem())
        show = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show Loqalyn", "showWindow:", "")
        show.setTarget_(self)
        menu.addItem_(show)
        refresh = C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh model library", None, "")
        self._action(refresh, lambda: self._refresh(True))
        menu.addItem_(refresh)
        menu.addItem_(C.NSMenuItem.separatorItem())
        menu.addItem_(C.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Loqalyn", "terminate:", ""))
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _page(self, name, title, subtitle, height):
        page = FlippedView.alloc().initWithFrame_(rect(0, 0, PAGE_WIDTH, height))
        page.layout_callback = None
        self.pages[name] = page
        self._label(page, name.upper(), 24, 12, PAGE_WIDTH - 48, 18, size=10, secondary=True, stretch=True)
        self._label(page, title, 24, 34, PAGE_WIDTH - 48, 35, size=27, bold=True, stretch=True)
        self._label(page, subtitle, 24, 77, PAGE_WIDTH - 48, 32, size=12, secondary=True, stretch=True)
        return page

    @objc.python_method
    def _layout(self):
        bounds = self.root.bounds()
        width, height = bounds.size.width, bounds.size.height
        self.sidebar.setFrameSize_(F.NSMakeSize(SIDEBAR,height))
        self.sidebar_health.setFrameOrigin_(F.NSMakePoint(25,height-192))
        self.sidebar_copy.setFrameOrigin_(F.NSMakePoint(14,height-95))
        self.sidebar_folder.setFrameOrigin_(F.NSMakePoint(14,height-53))
        self.main_scroll.setFrame_(rect(SIDEBAR + 8, 10, width - SIDEBAR - 18, height - 48))
        self.status_label.setFrame_(rect(SIDEBAR + 24, height - 30, width - SIDEBAR - 48, 22))
        document_width = self.main_scroll.contentSize().width
        for page in self.pages.values():
            page.setFrameSize_(F.NSMakeSize(document_width, page.frame().size.height))
        for index, card in enumerate(getattr(self, "metric_cards", [])):
            card_width=(document_width-48-42)/4
            card.setFrame_(rect(24+index*(card_width+14),240,card_width,96))

    @objc.python_method
    def _navigate(self, name, show=True):
        self.active_page = name
        for item, button in self.nav_buttons.items():
            button.setState_(C.NSControlStateValueOn if item == name else C.NSControlStateValueOff)
            button.setNeedsDisplay_(True)
        self.main_scroll.setDocumentView_(self.pages[name])
        self.main_scroll.contentView().scrollToPoint_(F.NSMakePoint(0, 0))
        self.main_scroll.reflectScrolledClipView_(self.main_scroll.contentView())
        self.window.setTitle_(name + " — Loqalyn")
        if show:
            self.showWindow_(None)

    @objc.python_method
    def _card(self, parent, x, y, width, height, stretch=True):
        card=FlippedView.alloc().initWithFrame_(rect(x,y,width,height))
        card.setWantsLayer_(True); card.layer().setBackgroundColor_(SURFACE.CGColor())
        card.layer().setCornerRadius_(12); card.layer().setBorderWidth_(1); card.layer().setBorderColor_(LINE.CGColor())
        if stretch: card.setAutoresizingMask_(C.NSViewWidthSizable)
        parent.addSubview_(card)
        return card

    @objc.python_method
    def _build_overview(self):
        page = self._page("Overview", "Your local AI, in sync.", "One private workspace for your models, conversations, and decisions.", 718)
        notice=self._card(page,24,117,PAGE_WIDTH-48,64)
        self._label(notice,"●",18,19,24,26,size=18).setTextColor_(ACCENT)
        self.overview_notice = self._label(notice, "Starting your local gateway…", 50, 14, PAGE_WIDTH-116, 43, size=12, stretch=True)
        self._button(page, "Open playground", 24, 194, 166, lambda: self._navigate("Playground"))
        self._button(page, "Find new models", 202, 194, 150, lambda: self._navigate("Discover"), "discover")
        self._button(page, "Retry startup", 364, 194, 130, self._start, "retry")
        self.metric_labels = {}; self.metric_cards=[]
        card_width=(PAGE_WIDTH-90)/4
        for index, (key, title) in enumerate((("served", "Recent completions"), ("models", "Models available"), ("warm", "Warm hit rate"), ("active", "Active requests"))):
            card=self._card(page,24+index*(card_width+14),240,card_width,96,False)
            self.metric_cards.append(card)
            self._label(card,title,16,15,card_width-32,22,secondary=True,size=11,stretch=True)
            self.metric_labels[key]=self._label(card,"—",16,42,card_width-32,39,size=29,bold=True,stretch=True)
        memory=self._card(page,24,356,PAGE_WIDTH-48,166)
        self._label(memory,"Memory budget",20,18,400,26,size=16,bold=True)
        self.memory_label=self._label(memory,"Waiting for the service",20,53,PAGE_WIDTH-90,28,size=12,stretch=True)
        self.memory_gauge=MemoryMeter.alloc().initWithFrame_(rect(20,91,PAGE_WIDTH-88,8))
        self.memory_gauge.setDoubleValue_(0); self.memory_gauge.setAutoresizingMask_(C.NSViewWidthSizable); memory.addSubview_(self.memory_gauge)
        self.overview_runtime=self._label(memory,"",20,117,PAGE_WIDTH-90,36,size=11,secondary=True,stretch=True)
        endpoint=self._card(page,24,542,PAGE_WIDTH-48,135)
        self._label(endpoint,"Connect your apps",20,18,500,25,size=16,bold=True)
        self._label(endpoint,self.controller.endpoint+"/v1",20,53,PAGE_WIDTH-90,24,size=14,stretch=True).setTextColor_(ACCENT)
        self._label(endpoint,"Choose auto, auto-fast, auto-code, or a specific model. Your API token is in Settings.",20,90,PAGE_WIDTH-90,34,size=11,secondary=True,stretch=True)

    @objc.python_method
    def _build_models(self):
        page = self._page("Models", "Your model library", "Discover installed weights from Ollama and LM Studio. Models stay in their existing runtimes.", 895)
        self.search_field = C.NSSearchField.alloc().initWithFrame_(rect(24, 113, 325, 28))
        self.search_field.setPlaceholderString_("Search models, runtimes, or tiers")
        self.search_field.setSendsSearchStringImmediately_(True)
        self.search_field.setTarget_(self)
        self.search_field.setAction_("filterModels:")
        page.addSubview_(self.search_field)
        self._button(page, "Refresh library", 365, 111, 135, lambda: self._refresh(True), "model_discover")
        self._button(page, "Find new models", 512, 111, 162, lambda: self._navigate("Discover"), "download")
        self._button(page, "Install by ID…", 686, 111, 155, self._download_model)
        columns = [("name", "Model", 235, lambda m: m.get("name", m["id"])), ("runtime", "Runtime", 110, lambda m: RUNTIME_NAMES.get(m["runtime"], m["runtime"])), ("memory", "Memory", 95, lambda m: f"{m.get('memory_gb', 0):.1f} GiB" if m.get("loaded") else "—"), ("context", "Max context", 100, lambda m: f"{m['context_max']:,}" if m.get("context_max") else "Unknown"), ("tier", "Tier", 100, lambda m: m.get("tier", "").replace("_", " ")), ("status", "State", 155, model_status)]
        self.model_table, self.model_source = self._table(page, columns, 157, 330, self._models_selected)
        self.model_count = self._label(page, "No inventory yet", 24, 498, PAGE_WIDTH - 48, secondary=True, stretch=True)
        for title, x, width, callback, key in (("Load", 24, 90, self._load_model, "load"), ("Unload", 124, 90, self._unload_model, "unload"), ("Configure…", 224, 125, self._configure_model, "configure_model"), ("Recover runtime…", 360, 175, self._recover_runtime, "recover")):
            self._button(page, title, x, 534, width, callback, key)
        self.model_details = self._editor(page, 24, 588, PAGE_WIDTH - 48, 238, editable=False)
        self.model_details.setString_("Select a model to inspect its runtime, memory estimate, and capabilities.")
        self._label(page, "Active, resident, and externally loaded models are protected. Memory values are admission estimates, not measured process peaks.", 24, 842, PAGE_WIDTH - 48, 42, secondary=True, size=11, stretch=True)

    @objc.python_method
    def _build_discover(self):
        page = self._page("Discover", "Find your next local model", "Search Hugging Face, choose GGUF weights, and install them directly into Ollama.", 1195)
        self.hub_query = self._field(page, "Model name or publisher/model", "", 24, 113, 562)
        self.hub_query.setPlaceholderString_("Try Qwen3, Llama, or a publisher/model repository")
        self._button(page, "Search Hugging Face", 602, 135, 255, self._search_hub, "hub_search")
        self._label(page, "Search sends only this search term to Hugging Face. Your prompts stay local. Public GGUF text models only.", 24, 185, PAGE_WIDTH-48, 36, secondary=True, size=11, stretch=True)
        columns = [("id", "Publisher / model", 535, lambda m:m["id"]), ("downloads", "Downloads", 130, lambda m:f"{m['downloads']:,}"), ("license", "License", 190, lambda m:m['license'])]
        self.hub_table, self.hub_source = self._table(page, columns, 235, 226, self._hub_selected)
        self._button(page, "Inspect files", 24, 480, 145, self._inspect_hub, "hub_inspect")
        self.hub_note = self._label(page, "Search for a model, then select a repository to inspect its download choices.", 186, 483, PAGE_WIDTH-210, 42, secondary=True, size=11, stretch=True)
        self._label(page, "Choose your weights", 24, 547, PAGE_WIDTH-48, 27, size=16, bold=True, stretch=True)
        columns = [("filename", "GGUF file", 515, lambda f:f['filename']), ("quant", "Precision", 115, lambda f:f['quant']), ("size", "Download", 105, lambda f:f"{f['size_gb']:.2f} GiB"), ("memory", "RAM estimate", 125, lambda f:f"~{f['memory_estimate_gb']:.1f} GiB")]
        self.hub_files_table, self.hub_files_source = self._table(page, columns, 590, 165, self._sync_buttons)
        self._button(page, "Download & install", 24, 777, 188, self._install_hub, "hub_install")
        self._button(page, "Model card & license", 224, 777, 195, self._open_hub_card, "hub_card")
        self._label(page, "Q4_K_M is a useful starting point when available. RAM estimates are rough; context and architecture change actual use. Ollama checks architecture support during installation.", 24, 831, PAGE_WIDTH-48, 50, secondary=True, size=11, stretch=True)
        self._label(page, "Downloads", 24, 916, 350, 27, size=16, bold=True)
        self._button(page, "Cancel downloads", 660, 909, 197, self._cancel_downloads, "hub_cancel")
        self.download_details = self._editor(page, 24, 963, PAGE_WIDTH-48, 158, editable=False)
        self.download_details.setString_("No downloads yet. Installed models will appear automatically in your library.")
        self._label(page, "Model publishers set their own licenses. Cancellation closes the download request; Ollama may retain partial files for reuse.", 24, 1138, PAGE_WIDTH-48, 37, secondary=True, size=11, stretch=True)

    @objc.python_method
    def _search_hub(self):
        query = str(self.hub_query.stringValue()).strip()
        self.hub_preview = None
        self._reload_table(self.hub_files_table, self.hub_files_source, [])
        self.hub_note.setStringValue_("Searching Hugging Face…")
        if '/' in query:
            self._work("hub-inspect", lambda:self.controller.call('/loqalyn/hub/inspect','POST',{'repository':query}), self._hub_inspected)
            return
        def done(result):
            self._reload_table(self.hub_table, self.hub_source, result['models'])
            self.hub_note.setStringValue_(f"{len(result['models'])} repositories. Select one, then inspect its files." if result['models'] else "No public GGUF text models found. Try a broader model name.")
        self._work("hub-search", lambda:self.controller.call('/loqalyn/hub/search','POST',{'query':query}), done)

    @objc.python_method
    def _hub_selected(self):
        self.hub_preview = None
        self._reload_table(self.hub_files_table, self.hub_files_source, [])
        self.hub_note.setStringValue_("Choose Inspect files to see sizes, precision options and the license.")
        self._sync_buttons()

    @objc.python_method
    def _inspect_hub(self):
        selected = self._selected(self.hub_table, self.hub_source)
        if not selected: raise ValueError("Select a Hugging Face repository first.")
        repo = selected['id']
        self.hub_preview = None
        self.hub_note.setStringValue_("Checking model files and download sizes…")
        self._work("hub-inspect", lambda:self.controller.call('/loqalyn/hub/inspect','POST',{'repository':repo}), self._hub_inspected)

    @objc.python_method
    def _hub_inspected(self, result):
        self.hub_preview = result
        self._reload_table(self.hub_files_table, self.hub_files_source, result['files'])
        self.hub_note.setStringValue_(result['repository'] + " · License: " + result['license'])
        if result['files']:
            self.hub_files_table.selectRowIndexes_byExtendingSelection_(F.NSIndexSet.indexSetWithIndex_(0),False)
        self._sync_buttons()

    @objc.python_method
    def _open_hub_card(self):
        if self.hub_preview:
            C.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.URLWithString_(self.hub_preview['url']))

    @objc.python_method
    def _install_hub(self):
        file = self._selected(self.hub_files_table, self.hub_files_source)
        if not file or not self.hub_preview: raise ValueError("Inspect a repository and select a GGUF file first.")
        preview = self.hub_preview
        budget = self.state['budget_gb'] - self.state['headroom_gb']
        text = (preview['repository'] + "\n" + file['filename'] + f"\n\nDownload: {file['size_gb']:.2f} GiB. Rough RAM estimate: {file['memory_estimate_gb']:.1f} GiB."
                + f"\nAllow roughly {file['size_gb']*2+1:.1f} GiB of free space in Ollama's storage."
                + "\nLicense: " + preview['license'] + ". Review the model card's terms before downloading."
                + "\n\nOllama will download and install these weights. No prompts are sent.")
        if file['memory_estimate_gb'] > budget:
            text += "\n\nThis estimate exceeds your usable model budget. Installation may succeed, but routing may exclude the model."
        if not self._confirm("Install this model?", text, "Download & install"): return
        body = {'preview_id':preview['id'], 'filename':file['filename'], 'confirmed':True}
        self._work("hub-install", lambda:self.controller.call('/loqalyn/hub/install','POST',body), self._download_started)

    @objc.python_method
    def _render_downloads(self):
        while len(self.download_states)>32: self.download_states.pop(next(iter(self.download_states)))
        lines = []
        for item in list(self.download_states.values())[-6:]:
            detail = item.get('error') or item.get('phase') or item.get('status','Starting')
            if item.get('total_bytes'):
                detail += f" · {item.get('percent',0):.1f}% · {item.get('completed_bytes',0)/1024**3:.2f} / {item['total_bytes']/1024**3:.2f} GiB"
            lines.append(item.get('model', 'Model download')+'\n'+detail)
        self.download_details.setString_('\n\n'.join(lines))

    @objc.python_method
    def _cancel_downloads(self):
        for job_id in tuple(self.job_ids):
            def done(result):
                self.download_states[result['id']] = result
                if result.get('status') != 'running': self.job_ids.discard(result['id'])
                self._render_downloads()
            self._work('cancel-'+job_id, lambda ident=job_id:self.controller.call('/loqalyn/jobs/'+quote(ident,safe='')+'/cancel','POST',{}), done)

    @objc.python_method
    def _verify_local_setup(self):
        if self._settings_values() != self.settings_form_snapshot:
            raise ValueError("Save your edited settings before verifying local setup.")
        def checked(state):
            self._apply_state(state)
            policy = state['policy']
            if set(policy['runtimes']) == {'ollama'} and state.get('runtimes',{}).get('ollama',{}).get('cloud_disabled') is True:
                updated = copy.deepcopy(policy); updated['runtime_local_only_confirmed'] = True
                self._work('settings', lambda:self.controller.call('/loqalyn/policy','PUT',updated), lambda _:self._refresh(True))
                self._status("Ollama confirms cloud access is off. Local generation is enabled.")
            else:
                self._status("Review the verification result in Local runtime setup. Confirm LM Link or older runtime settings manually.")
        self._work('verify-setup', lambda:self.controller.call('/loqalyn/refresh','POST',{}), checked)

    @objc.python_method
    def _build_playground(self):
        page = self._page("Playground", "Take the router for a spin", "Evaluate a route without loading weights, or stream a response from a local model.", 1025)
        self._label(page, "Model", 24, 114, 420, secondary=True, size=11)
        self.play_model = self._popup(page, [("Auto · best fit", "auto"), ("Auto · fast", "auto-fast"), ("Auto · code", "auto-code")], 24, 137, 390)
        self.play_limit = self._field(page, "Maximum output tokens", "1024", 444, 114, 215)
        self._label(page, "Your prompt", 24, 186, PAGE_WIDTH - 48, secondary=True, size=11, stretch=True)
        self.play_prompt = self._editor(page, 24, 213, PAGE_WIDTH - 48, 140)
        self.play_tools = self._check(page, "Require verified tool calling", 470, 365, 360)
        self._button(page, "Evaluate route", 24, 366, 150, self._route_request, "route")
        self._button(page, "Generate answer", 186, 366, 160, self._generate, "generate")
        self._button(page, "Stop", 358, 366, 85, self._stop_generation, "stop")
        self.play_status = self._label(page, "No request yet. Tool calls are displayed but never executed by the playground.", 24, 412, PAGE_WIDTH - 48, 32, secondary=True, size=11, stretch=True)
        self._label(page, "Why this model", 24, 758, PAGE_WIDTH - 48, 28, size=16, bold=True, stretch=True)
        self.play_trace = self._editor(page, 24, 792, PAGE_WIDTH - 48, 190, editable=False, mono=True)
        self.play_trace.setString_("Your route's candidates, exclusions, model, and reason will appear here.")
        self._label(page, "Response", 24, 456, 230, 28, size=16, bold=True)
        self.show_reasoning = self._check(page, "Show model reasoning", 610, 456, 270)
        self._action(self.show_reasoning, self._render_stream)
        self.play_output = self._editor(page, 24, 490, PAGE_WIDTH - 48, 240, editable=False)

    @objc.python_method
    def _build_policies(self):
        page = self._page("Policies", "Routing policy", "Editable YAML. Hard constraints always apply; quick_chat remains the fallback.", 870)
        self._button(page, "Save policy", 24, 115, 135, self._save_policy, "save_policy")
        self._button(page, "Reload", 172, 115, 95, self._reload_policy, "reload_policy")
        self._button(page, "Export…", 280, 115, 105, self._export_policy, "export_policy")
        self.policy_note = self._label(page, "Policy changes apply to future decisions without restarting the app.", 24, 158, PAGE_WIDTH - 48, 45, secondary=True, size=11, stretch=True)
        self.policy_editor = self._editor(page, 24, 211, PAGE_WIDTH - 48, 607, mono=True)

    @objc.python_method
    def _build_activity(self):
        page = self._page("Activity", "Every decision, explained", "The local decision journal stores routing metadata, not prompt content or model responses.", 860)
        self._button(page, "Refresh", 24, 113, 105, lambda: self._refresh(False), "activity_refresh")
        self._button(page, "Export decisions…", 142, 113, 170, self._export_activity, "export_activity")
        columns = [("time", "When", 140, lambda d: format_time(d.get("created"))), ("route", "Route", 165, lambda d: ROUTE_NAMES.get(d.get("route"), d.get("route", ""))), ("model", "Model", 225, lambda d: d.get("model_name") or d.get("model") or "None"), ("status", "Status", 100, lambda d: d.get("status", "")), ("ms", "Routing", 80, lambda d: f"{d.get('duration_ms', 0)} ms"), ("flag", "Review flag", 165, lambda d: (d.get("escalation") or "—").replace("_", " "))]
        self.activity_table, self.activity_source = self._table(page, columns, 158, 292, self._activity_selected)
        self.activity_details = self._editor(page, 24, 478, PAGE_WIDTH - 48, 332, editable=False, mono=True)
        self.activity_details.setString_("Select a request to inspect its decision.")

    @objc.python_method
    def _build_escalations(self):
        page = self._page("Escalations", "You have the final say", "Review, redact, preview the exact outgoing request, then approve one provider call. Nothing is sent by a routing suggestion.", 1330)
        columns = [("time", "When", 140, lambda p: format_time(p.get("created"))), ("route", "Route", 175, lambda p: ROUTE_NAMES.get(p.get("route"), p.get("route", ""))), ("status", "Status", 100, lambda p: p.get("status", "")), ("reason", "Reason", 210, lambda p: p.get("reason", "").replace("_", " ")), ("tags", "Sensitive tags", 165, lambda p: ", ".join(p.get("tags", [])) or "—")]
        self.escalation_table, self.escalation_source = self._table(page, columns, 117, 165, self._escalation_selected)
        self.review_note = self._label(page, "Select a request. Pending payloads expire after 30 minutes and are not persisted in the decision journal.", 24, 294, PAGE_WIDTH - 48, 43, secondary=True, size=11, stretch=True)
        self.review_provider = self._popup(page, [("OpenAI", "openai"), ("Anthropic", "anthropic")], 24, 363, 170)
        self._action(self.review_provider, self._invalidate_review)
        self._label(page, "Provider", 24, 341, 170, secondary=True, size=11)
        self.review_model = self._field(page, "Provider model ID", "", 214, 340, 405)
        self.review_model.setDelegate_(self)
        self._label(page, "Editable request JSON · redact sensitive information here", 24, 411, PAGE_WIDTH - 48, 25, secondary=True, size=11, stretch=True)
        self.review_payload = self._editor(page, 24, 443, PAGE_WIDTH - 48, 180, mono=True)
        self.review_payload.setDelegate_(self)
        self._button(page, "Preview outgoing request", 24, 637, 218, self._preview_escalation, "preview")
        self._button(page, "Keep local", 254, 637, 125, self._deny_escalation, "deny")
        self._label(page, "Exact outgoing payload", 24, 687, PAGE_WIDTH - 48, 28, bold=True, size=16, stretch=True)
        self.review_outgoing = self._editor(page, 24, 723, PAGE_WIDTH - 48, 194, editable=False, mono=True)
        self.review_hash = self._label(page, "Preview before approval. Changing the request, provider, or model invalidates the preview.", 24, 928, PAGE_WIDTH - 48, 48, secondary=True, size=10, stretch=True)
        self.review_confirm = self._check(page, "I reviewed the exact outgoing payload and authorize this one request.", 24, 982, PAGE_WIDTH - 48, callback=self._sync_buttons)
        self._button(page, "Approve & send", 24, 1020, 163, self._approve_escalation, "approve")
        self._label(page, "Provider response", 24, 1069, PAGE_WIDTH - 48, 28, bold=True, size=16, stretch=True)
        self.review_response = self._editor(page, 24, 1105, PAGE_WIDTH - 48, 187, editable=False, mono=True)

    @objc.python_method
    def _build_settings(self):
        page = self._page("Settings", "Your local workspace", "Configure memory admission, runtime privacy, and optional frontier credentials.", 1350)
        self._button(page, "Save settings", 24, 114, 145, self._save_settings, "save_settings")
        self._button(page, "Reload settings", 181, 114, 150, self._reload_settings, "reload_settings")
        self._label(page, "Memory and retention", 24, 173, PAGE_WIDTH - 48, 26, bold=True, size=16, stretch=True)
        self.settings_fields = {}
        for key, title, x, y, width in (("budget_gb", "Model budget (GiB)", 24, 212, 188), ("headroom_gb", "Admission headroom (GiB)", 234, 212, 195), ("default_max_tokens", "Default output tokens", 451, 212, 198), ("log_retention_days", "Decision retention (days)", 672, 212, 196)):
            self.settings_fields[key] = self._field(page, title, "", x, y, width)
        self._label(page, "Reserve headroom within the budget. Unload models before lowering the budget below current reservations.", 24, 279, PAGE_WIDTH - 48, 39, secondary=True, size=11, stretch=True)
        self._label(page, "Local runtime setup", 24, 331, PAGE_WIDTH - 48, 27, bold=True, size=16, stretch=True)
        self.locality_confirm = self._check(page, "I disabled cloud forwarding in every enabled runtime.", 24, 370, PAGE_WIDTH - 48)
        self.locality_status = self._label(page, "Ollama cloud settings are checked before generation. Disable LM Link manually if you use LM Studio.", 24, 407, PAGE_WIDTH - 48, 51, secondary=True, size=11, stretch=True)
        self.runtime_enabled = {}
        for index, runtime in enumerate(RUNTIME_NAMES):
            y = 480 + index * 79
            self.runtime_enabled[runtime] = self._check(page, "Enable " + RUNTIME_NAMES[runtime], 24, y + 22, 195)
            self.settings_fields[runtime] = self._field(page, "Loopback HTTP URL", "", 239, y, 462)
        self._button(page, "Verify local setup", 24, 647, 220, self._verify_local_setup, "settings_discover")
        self._label(page, "Save edited addresses before checking. Unload all models before changing runtime URLs.", 262, 651, PAGE_WIDTH - 286, 44, secondary=True, size=11, stretch=True)
        self._label(page, "Privacy and escalation", 24, 714, PAGE_WIDTH - 48, 27, bold=True, size=16, stretch=True)
        self.settings_mode = self._popup(page, [("Flag · review required", "flag"), ("Off · local only", "off")], 24, 753, 260)
        self._label(page, "Sensitive terms · one per line", 310, 730, PAGE_WIDTH - 334, secondary=True, size=11, stretch=True)
        self.settings_terms = self._editor(page, 310, 757, PAGE_WIDTH - 334, 102)
        self._label(page, "Detection is heuristic. Review the entire outgoing request; detected sensitive content must be redacted before approval.", 24, 878, PAGE_WIDTH - 48, 43, secondary=True, size=11, stretch=True)
        self._label(page, "Keys and runtime authentication", 24, 942, PAGE_WIDTH - 48, 27, bold=True, size=16, stretch=True)
        self.key_provider = self._popup(page, [("OpenAI", "openai"), ("Anthropic", "anthropic"), ("LM Studio token", "lmstudio")], 24, 1005, 170)
        self.key_field = self._field(page, "API key or server token · saved in macOS Keychain", "", 214, 981, 435, secure=True)
        self._button(page, "Save key", 667, 1002, 120, self._save_key, "save_key")
        self._label(page, "Frontier keys are optional. A protected LM Studio server may need its local API token. Saving a secret does not send a model request; existing secrets are never displayed.", 24, 1053, PAGE_WIDTH - 48, 42, secondary=True, size=11, stretch=True)
        self._label(page, "Client connection", 24, 1120, PAGE_WIDTH - 48, 27, bold=True, size=16, stretch=True)
        self._label(page, self.controller.endpoint + "/v1", 24, 1160, PAGE_WIDTH - 48, 24, stretch=True)
        self._button(page, "Copy base URL", 24, 1200, 150, self._copy_endpoint)
        self._button(page, "Copy API token", 186, 1200, 155, self._copy_token, "copy_token")
        self._label(page, "Closing the window keeps Loqalyn in the menu bar. Quit Loqalyn to stop its local endpoint.", 24, 1257, PAGE_WIDTH - 48, 47, secondary=True, size=11, stretch=True)

    @objc.python_method
    def _status(self, text):
        self.status_label.setStringValue_(text)

    @objc.python_method
    def _error(self, message):
        self._status(message)
        alert = C.NSAlert.alloc().init()
        alert.setAlertStyle_(C.NSAlertStyleWarning)
        alert.setMessageText_("Loqalyn could not complete this action")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    @objc.python_method
    def _confirm(self, title, message, verb, destructive=False):
        alert = C.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.setAlertStyle_(C.NSAlertStyleWarning if destructive else C.NSAlertStyleInformational)
        alert.addButtonWithTitle_("Cancel" if not destructive else "Keep Running")
        alert.addButtonWithTitle_(verb)
        return alert.runModal() == C.NSAlertSecondButtonReturn

    @objc.python_method
    def _work(self, key, function, success=None, *, quiet=False, allow_quit=False):
        if key in self.pending or (self.quitting and not allow_quit):
            return
        self.pending.add(key)
        self._sync_buttons()

        def worker():
            with objc.autorelease_pool():
                try:
                    result, error = function(), None
                except Exception as exc:
                    result, error = None, exc
                AppHelper.callAfter(self._work_done, key, result, error, success, quiet)

        threading.Thread(target=worker, name="Loqalyn-" + key, daemon=True).start()

    @objc.python_method
    def _work_done(self, key, result, error, success, quiet):
        self.pending.discard(key)
        if self.quitting and key not in ("shutdown", "quit-check"):
            # Replay these only if the user cancels quitting; no modal error
            # should interrupt the shutdown confirmation or service teardown.
            self.deferred_callbacks.append((key, result, error, success, quiet))
            return
        if error:
            if key == "quit-check":
                if self._confirm("Quit Loqalyn?", "The service's active-request status could not be read. Quitting may interrupt requests. " + str(error), "Quit", destructive=True):
                    self._begin_shutdown()
                else:
                    self._cancel_quit()
            elif key == "shutdown":
                self._cancel_quit()
                self._error("The local service did not stop cleanly: " + str(error))
            elif key == "startup":
                self.starting = False
                self.overview_notice.setStringValue_("Startup failed: " + str(error) + "\nChoose Retry startup after resolving the issue.")
                self.sidebar_health.setStringValue_("Service unavailable")
                self.status_menu_line.setTitle_("Service unavailable")
                self._status("Startup failed: " + str(error))
            elif quiet:
                self.last_connection_error = str(error)
                self._status("Connection issue: " + str(error))
            else:
                if key == 'claude-verify': self.claude_check_note.setStringValue_('Tool check failed. '+str(error)[:180])
                if key == "generation":
                    self.stream_cancel = None
                    self.play_status.setStringValue_("Generation stopped: " + str(error))
                    details = getattr(error, "details", None)
                    if isinstance(details, dict) and isinstance(details.get("details"), dict):
                        details = details["details"]
                    if isinstance(details, dict) and "candidates" in details:
                        self.play_trace.setString_(decision_text(details))
                    self._refresh(False, quiet=True)
                self._error(str(error))
        elif success:
            try:
                success(result)
            except Exception as exc:
                self._error(str(exc))
        self._sync_buttons()

    @objc.python_method
    def _start(self):
        if self.started or self.starting:
            return
        self.starting = True
        self.overview_notice.setStringValue_("Starting the local gateway and discovering installed models…")
        self._status("Starting local service…")
        self._work("startup", self.controller.start, self._started)

    @objc.python_method
    def _started(self, state):
        self.starting = False
        self.started = True
        self._apply_state(state)
        self._status("Local service ready at " + self.controller.endpoint)
        self._check_claude()

    @objc.python_method
    def _refresh(self, discover=False, quiet=False):
        if not self.started:
            if not quiet:
                self._start()
            return
        path = "/loqalyn/refresh" if discover else "/loqalyn/status"
        self._work("refresh", lambda: self.controller.call(path, "POST" if discover else "GET", {} if discover else None), self._apply_state, quiet=quiet)
        if not quiet:
            self._status("Discovering runtime models…" if discover else "Refreshing workspace…")

    @objc.python_method
    def _apply_state(self, state):
        if not isinstance(state, dict) or "models" not in state or "policy" not in state:
            raise ValueError("The service returned an invalid workspace state.")
        self.state = state
        self.last_connection_error = None
        models = state.get("models", [])
        decisions = state.get("decisions", [])
        completed = [d for d in decisions if d.get("status") == "completed"]
        for key, value in {"served": len(completed), "models": len(models), "warm": f"{state['warm_rate']}%" if state.get("warm_rate") is not None else "—", "active": state.get("active", 0)}.items():
            self.metric_labels[key].setStringValue_(str(value))
        used, budget, headroom = state.get("used_gb", 0), state.get("budget_gb", 0), state.get("headroom_gb", 0)
        self.memory_label.setStringValue_(f"{used:.1f} / {budget:.1f} GiB reserved  •  {headroom:.1f} GiB headroom  •  {max(0, budget - headroom - used):.1f} GiB available")
        self.memory_gauge.setDoubleValue_(min(100, used / budget * 100) if budget else 0)
        health = state.get("runtimes", {})
        runtime_lines = []
        for runtime, name in RUNTIME_NAMES.items():
            item = health.get(runtime)
            runtime_lines.append(name + ": " + ("Not configured" if item is None else "Online" if item.get("online") else "Offline") + ((" — " + str(item["error"])) if item and item.get("error") else ""))
        self.sidebar_health.setStringValue_("\n\n".join(line.split(" — ")[0] for line in runtime_lines))
        self.overview_runtime.setStringValue_("    •    ".join(runtime_lines))
        ollama = health.get("ollama", {})
        privacy = "Ollama: cloud access verified off." if ollama.get("cloud_disabled") is True else "Ollama: cloud access is enabled. Disable it and restart Ollama." if ollama.get("cloud_disabled") is False else "Ollama: cloud setting could not be verified. Confirm setup manually if your version does not report it."
        if "ollama" not in state["policy"]["runtimes"]: privacy = "Ollama is not enabled."
        self.locality_status.setStringValue_(privacy + (" LM Studio: disable LM Link and confirm manually." if "lmstudio" in state["policy"]["runtimes"] else ""))
        if state.get("config_error"):
            self.overview_notice.setStringValue_("Configuration needs attention: " + state["config_error"])
        elif not state["policy"].get("runtime_local_only_confirmed"):
            self.overview_notice.setStringValue_("Finish local setup in Settings, then choose Verify local setup. Disable Ollama cloud access; disable LM Link if you use LM Studio. You can browse models and preview routes now.")
        elif health.get("ollama", {}).get("cloud_disabled") is False:
            self.overview_notice.setStringValue_("Ollama cloud access is enabled. Generation through Ollama is blocked until you disable it and restart Ollama.")
        elif not models:
            self.overview_notice.setStringValue_("No installed models found. Start your enabled runtime. Choose Discover to find and install a model, or refresh the Models library if you already have one.")
        else:
            self.overview_notice.setStringValue_("Local gateway ready. Model loads stay within your admission budget. Cloud requests always require approval.")
        self.status_menu_line.setTitle_(f"{state.get('active', 0)} active • {sum(bool(m.get('loaded')) for m in models)} loaded • {used:.1f} GiB")
        self._reload_models()
        self._reload_table(self.activity_table, self.activity_source, decisions)
        self._reload_table(self.escalation_table, self.escalation_source, state.get("escalations", []))
        selected=next((p for p in state.get('escalations',[]) if p['id']==self.review_request_id),None)
        if self.review_request_id and (not selected or selected.get('status')!='pending'):
            self._clear_review()
        self._update_play_models()
        self._update_claude()
        if not self.policy_loaded or str(self.policy_editor.string()) == self.policy_editor_snapshot:
            self._fill_policy(state["policy"])
        if not self.settings_loaded or self._settings_values() == self.settings_form_snapshot:
            self._fill_settings(state["policy"])
        if not state["policy"].get("runtime_local_only_confirmed") and self.stream_cancel is None:
            self.play_status.setStringValue_("Finish local setup in Settings, then choose Verify local setup to enable generation.")
        self._sync_buttons()

    @objc.python_method
    def _reload_table(self, table, source, rows):
        if source.rows == list(rows): return
        current = self._selected(table, source)
        identifier = current.get("id") if current else None
        source.suppress_selection = True
        try:
            source.rows = list(rows)
            table.reloadData()
            index = next((i for i, item in enumerate(rows) if item.get("id") == identifier), None)
            if index is None:
                table.deselectAll_(None)
            else:
                table.selectRowIndexes_byExtendingSelection_(F.NSIndexSet.indexSetWithIndex_(index), False)
        finally:
            source.suppress_selection = False

    @objc.python_method
    def _selected(self, table, source):
        index = table.selectedRow()
        return source.rows[index] if 0 <= index < len(source.rows) else None

    @objc.python_method
    def _reload_models(self):
        if self.state is None:
            return
        rows = filtered_models(self.state, str(self.search_field.stringValue()))
        self._reload_table(self.model_table, self.model_source, rows)
        self.model_count.setStringValue_(f"{len(rows)} model(s) shown of {len(self.state.get('models', []))} discovered")
        self._models_selected()

    @objc.python_method
    def _models_selected(self):
        model = self._selected(self.model_table, self.model_source)
        self.model_details.setString_(model_summary(model) if model else "Select a model to inspect its runtime, memory estimate, and capabilities.")
        self._sync_buttons()

    @objc.python_method
    def _activity_selected(self):
        decision = self._selected(self.activity_table, self.activity_source)
        self.activity_details.setString_(decision_text(decision) if decision else "Select a request to inspect its decision.")

    @objc.python_method
    def _sync_buttons(self):
        if not self.controls or not self.ui_ready:
            return
        live = self.started and not self.quitting
        for name, button in self.controls.items():
            button.setEnabled_(live)
            button.setNeedsDisplay_(True)
        self.controls["retry"].setEnabled_(not self.started and not self.starting and not self.quitting)
        self.controls["retry"].setHidden_(self.started)
        model = self._selected(self.model_table, self.model_source) if hasattr(self, "model_table") else None
        mutating = "model" in self.pending
        self.controls["load"].setEnabled_(bool(live and model and not mutating and not load_blocker(model, self.state or {})))
        self.controls["unload"].setEnabled_(bool(live and model and not mutating and not unload_blocker(model)))
        self.controls["configure_model"].setEnabled_(bool(live and model and not mutating and not model.get("active")))
        self.controls["recover"].setEnabled_(bool(live and model and model.get("state_uncertain") and not mutating))
        self.controls["route"].setEnabled_(live and "route" not in self.pending and self.stream_cancel is None)
        self.controls["generate"].setEnabled_(bool(live and (self.state or {}).get("policy", {}).get("runtime_local_only_confirmed") and self.stream_cancel is None and "route" not in self.pending))
        self.controls["stop"].setEnabled_(self.stream_cancel is not None)
        self.controls["hub_search"].setEnabled_(live and "hub-search" not in self.pending and "hub-inspect" not in self.pending)
        self.controls["hub_inspect"].setEnabled_(bool(live and self._selected(self.hub_table,self.hub_source) and "hub-inspect" not in self.pending))
        self.controls["hub_install"].setEnabled_(bool(live and self.hub_preview and self._selected(self.hub_files_table,self.hub_files_source) and "hub-install" not in self.pending and "ollama" in (self.state or {}).get("policy",{}).get("runtimes",{})))
        self.controls["hub_cancel"].setEnabled_(bool(live and self.job_ids))
        self.controls["hub_card"].setEnabled_(bool(live and self.hub_preview))
        self.controls["save_policy"].setEnabled_(live and "policy" not in self.pending)
        self.controls["save_settings"].setEnabled_(live and "settings" not in self.pending)
        self.controls["save_key"].setEnabled_(live and "key" not in self.pending)
        coding=(self.state or {}).get('claude_code',{})
        selected=self._selected(self.claude_models,self.claude_models_source)
        self.controls['claude_verify'].setEnabled_(bool(live and selected and (selected['can_verify'] or selected['ready']) and 'claude-verify' not in self.pending))
        self.controls['claude_launch'].setEnabled_(bool(live and coding.get('ready') and self.claude_client.get('compatible') and self.claude_project and 'claude-launch' not in self.pending))
        row = self._selected(self.escalation_table, self.escalation_source) if hasattr(self, "escalation_table") else None
        pending = bool(live and row and row.get("status") == "pending" and self.review_request_id == row.get("id") and "review" not in self.pending)
        self.controls["preview"].setEnabled_(pending)
        self.controls["deny"].setEnabled_(pending)
        self.controls["approve"].setEnabled_(bool(pending and self.review and not self.review.preview.get("tags") and self.review_confirm.state() == C.NSControlStateValueOn))

    @objc.python_method
    def _chosen_model(self):
        model = self._selected(self.model_table, self.model_source)
        if not model:
            raise ValueError("Select a model first.")
        return model

    @objc.python_method
    def _load_model(self):
        model = self._chosen_model()
        reason = load_blocker(model, self.state)
        if reason:
            raise ValueError(reason)
        body = {"model": model["id"], "context": min(8192, model["context_max"])}
        self._status("Loading " + model["name"] + "…")
        self._work("model", lambda: self.controller.call("/loqalyn/models/load", "POST", body), self._model_changed)

    @objc.python_method
    def _unload_model(self):
        model = self._chosen_model()
        reason = unload_blocker(model)
        if reason:
            raise ValueError(reason)
        self._status("Unloading " + model["name"] + "…")
        self._work("model", lambda: self.controller.call("/loqalyn/models/unload", "POST", {"model": model["id"]}), self._model_changed)

    @objc.python_method
    def _model_changed(self, state):
        self._apply_state(state)
        self._status("Model state updated.")

    @objc.python_method
    def _configure_model(self):
        model = copy.deepcopy(self._chosen_model())
        accessory = FlippedView.alloc().initWithFrame_(rect(0, 0, 585, 385))
        accessory.layout_callback = None
        fields = {}
        fields["name"] = self._field(accessory, "Display name", model.get("name", ""), 0, 0, 580)
        self._label(accessory, "Memory tier", 0, 68, 240, secondary=True, size=11)
        tier = self._popup(accessory, [("Resident", "resident"), ("On demand", "on_demand"), ("Archive", "archive")], 0, 93, 245)
        self._select_popup(tier, model.get("tier"))
        for key, title, x, y, width in (("size_gb", "Weight size (GiB)", 0, 143, 180), ("context_max", "Maximum context", 202, 143, 180), ("kv_gb_per_1k", "KV GiB / 1024 tokens", 405, 143, 180)):
            fields[key] = self._field(accessory, title, model.get(key, ""), x, y, width)
        verified = self._check(accessory, "I verified tool calling on this model.", 0, 220, 580, model.get("tools_verified", False))
        self._label(accessory, "Use measured values or model architecture metadata for overrides. Advertised tool capability is not a verification. Resident models are protected and reloaded while the service is running.", 0, 264, 580, 87, secondary=True, size=11)
        alert = C.NSAlert.alloc().init()
        alert.setMessageText_("Configure " + model.get("name", model["id"]))
        alert.setInformativeText_(model["id"])
        alert.setAccessoryView_(accessory)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != C.NSAlertFirstButtonReturn:
            return
        values = {key: str(field.stringValue()) for key, field in fields.items()}
        values.update(tier=self._popup_value(tier), tools_verified=verified.state() == C.NSControlStateValueOn)
        patch = model_patch(values)
        self._work("model", lambda: self.controller.call("/loqalyn/models/configure", "POST", {"model": model["id"], "values": patch}), self._model_changed)

    @objc.python_method
    def _recover_runtime(self):
        model = self._chosen_model()
        runtime = model["runtime"]
        if self._confirm("Recover " + RUNTIME_NAMES.get(runtime, runtime) + "?", "First quit and restart this runtime. Recovery rechecks its inventory and clears uncertain reservations only after that explicit restart. Have you restarted it?", "I restarted it; recover"):
            self._work("model", lambda: self.controller.call("/loqalyn/recover", "POST", {"runtime": runtime, "confirmed_runtime_restarted": True}), self._model_changed)

    @objc.python_method
    def _download_model(self):
        accessory = FlippedView.alloc().initWithFrame_(rect(0, 0, 550, 180))
        accessory.layout_callback = None
        runtime = self._popup(accessory, [("Ollama", "ollama"), ("LM Studio", "lmstudio")], 0, 0, 220)
        identifier = self._field(accessory, "Model identifier", "", 0, 50, 545)
        identifier.setPlaceholderString_("For example: qwen3:8b")
        confirm = self._check(accessory, "I authorize downloading these weights through the selected runtime.", 0, 122, 550)
        alert = C.NSAlert.alloc().init()
        alert.setMessageText_("Download model weights")
        alert.setInformativeText_("This is a network download. The runtime chooses the model source and keeps the weights in its existing storage.")
        alert.setAccessoryView_(accessory)
        alert.addButtonWithTitle_("Download")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != C.NSAlertFirstButtonReturn:
            return
        name = str(identifier.stringValue()).strip()
        if not name or name.startswith("-") or len(name) > 250:
            raise ValueError("Enter a valid model identifier.")
        if confirm.state() != C.NSControlStateValueOn:
            raise ValueError("Select the download authorization checkbox first.")
        body = {"runtime": self._popup_value(runtime), "model": name, "confirmed": True}
        self._work("download", lambda: self.controller.call("/loqalyn/pull", "POST", body), self._download_started)

    @objc.python_method
    def _download_started(self, result):
        self.job_ids.add(result["id"])
        self._status("Download started. Loqalyn will check progress while it runs.")
        self.download_states[result["id"]] = result
        self._render_downloads()

    @objc.python_method
    def _poll_job(self, job_id):
        def done(result):
            self.download_states[result["id"]] = result
            self._render_downloads()
            status = result.get("status")
            if status != "running":
                self.job_ids.discard(job_id)
                self._status("Download " + str(status) + (": " + str(result["error"]) if result.get("error") else "."))
                self._refresh(True, quiet=True)
        self._work("job-" + job_id, lambda: self.controller.call("/loqalyn/jobs/" + quote(job_id, safe="")), done, quiet=True)

    @objc.python_method
    def _update_play_models(self):
        selected = self._popup_value(self.play_model)
        self.play_model.removeAllItems()
        items = [("Auto · best fit", "auto"), ("Auto · fast", "auto-fast"), ("Auto · code", "auto-code")]
        items.extend((model.get("name", model["id"]) + " · " + RUNTIME_NAMES.get(model["runtime"], model["runtime"]), model["id"]) for model in self.state.get("models", []))
        for title, value in items:
            self.play_model.addItemWithTitle_(title)
            self.play_model.lastItem().setRepresentedObject_(value)
        self._select_popup(self.play_model, selected)

    @objc.python_method
    def _play_body(self):
        return request_payload(str(self.play_prompt.string()), self._popup_value(self.play_model), str(self.play_limit.stringValue()), self.play_tools.state() == C.NSControlStateValueOn)

    @objc.python_method
    def _route_request(self):
        body = self._play_body()
        def done(result):
            self.play_trace.setString_(decision_text(result))
            self.play_status.setStringValue_("Route evaluated. No generation, loading, or provider request was performed.")
        self._work("route", lambda: self.controller.call("/loqalyn/route", "POST", body), done)

    @objc.python_method
    def _generate(self):
        if self.stream_cancel is not None:
            return
        if not self.state["policy"].get("runtime_local_only_confirmed"):
            raise ValueError("Confirm local runtime setup in Settings before generating.")
        body = self._play_body()
        self.stream_cancel = threading.Event()
        cancel = self.stream_cancel
        self.stream_buffer = StreamBuffer()
        self.last_stream_text = None
        self.play_output.setString_("")
        self.play_status.setStringValue_("Routing and warming a local model…")
        self._work("generation", lambda: self.controller.stream(body, self._receive_delta, cancel), self._generation_done)

    @objc.python_method
    def _receive_delta(self, chunk):
        # Called on the controller worker. Cocoa is updated only by callAfter.
        with self.stream_lock:
            self.stream_buffer.add(chunk)
            if not self.stream_render_pending:
                self.stream_render_pending = True
                with objc.autorelease_pool():
                    AppHelper.callAfter(self._schedule_stream_render)

    @objc.python_method
    def _schedule_stream_render(self):
        # At most ten whole-text updates per second, including reasoning tokens.
        AppHelper.callLater(max(0, 0.1-(time.monotonic()-self.last_stream_render)), self._render_stream)

    @objc.python_method
    def _render_stream(self):
        with self.stream_lock:
            self.stream_render_pending = False
            if self.quitting:
                return
            text = self.stream_buffer.text(self.show_reasoning.state() == C.NSControlStateValueOn)
            thinking = bool(self.stream_buffer.reasoning and not self.stream_buffer.content)
            model = self.stream_buffer.model
        self.last_stream_render = time.monotonic()
        if text != self.last_stream_text:
            self.play_output.setString_(text)
            self.last_stream_text = text
        if self.stream_cancel is not None:
            self.play_status.setStringValue_((model + " · " if model else "") + ("Thinking locally… Your answer will appear here." if thinking else "Generating locally…"))

    @objc.python_method
    def _generation_done(self, result):
        self.stream_cancel = None
        self._render_stream()
        result = result or {}
        if not self.play_output.string() and result.get("content"):
            self.play_output.setString_(result["content"])
        status = "Cancelled; partial output retained." if result.get("cancelled") else "Output limit reached." if result.get("finish_reason") == "length" else "Tool call requested; not executed by the playground." if result.get("finish_reason") == "tool_calls" else "Complete."
        self.play_status.setStringValue_((str(result["model"]) + " · " if result.get("model") else "") + status)
        request_id = result.get("request_id")
        if request_id and (self.state or {}).get('policy',{}).get('log_retention_days',30)>0:
            self._work("generation-decision", lambda: self.controller.call("/loqalyn/decisions/" + quote(request_id, safe="")), lambda decision: self.play_trace.setString_(decision_text(decision)), quiet=True)
        elif request_id:
            self.play_trace.setString_("Decision history is disabled in Settings. Use Evaluate route to inspect a route without saving it.")
        self._refresh(False, quiet=True)

    @objc.python_method
    def _stop_generation(self):
        if self.stream_cancel is not None:
            self.stream_cancel.set()
            self.play_status.setStringValue_("Stopping generation… Partial output will be retained.")

    @objc.python_method
    def _fill_policy(self, policy):
        self.policy_editor_snapshot = yaml.safe_dump(policy, sort_keys=False, allow_unicode=True)
        self.policy_base = copy.deepcopy(policy)
        self.policy_editor.setString_(self.policy_editor_snapshot)
        self.policy_loaded = True

    @objc.python_method
    def _save_policy(self):
        policy = policy_yaml(str(self.policy_editor.string()))
        baseline = copy.deepcopy(self.policy_base)
        def save():
            if self.controller.call('/loqalyn/policy') != baseline:
                raise ValueError('Saved settings changed while this policy was being edited. Reload the policy before saving.')
            return self.controller.call('/loqalyn/policy', 'PUT', policy)
        def done(result):
            self._fill_policy(result)
            self.policy_note.setStringValue_("Policy saved. Future requests use these settings.")
            self._refresh(False, quiet=True)
        self._work("policy", save, done)

    @objc.python_method
    def _reload_policy(self):
        if self._confirm("Reload saved policy?", "This replaces the YAML editor with the current saved policy. Unsaved editor changes will be discarded.", "Reload"):
            self._work("policy", lambda: self.controller.call("/loqalyn/policy"), self._fill_policy)

    @objc.python_method
    def _fill_settings(self, policy):
        for key in ("budget_gb", "headroom_gb", "default_max_tokens", "log_retention_days"):
            self.settings_fields[key].setStringValue_(str(policy.get(key, "")))
        defaults = {"ollama": "http://127.0.0.1:11434", "lmstudio": "http://127.0.0.1:1234"}
        for runtime in RUNTIME_NAMES:
            self.settings_fields[runtime].setStringValue_(policy.get("runtimes", {}).get(runtime, defaults[runtime]))
            self.runtime_enabled[runtime].setState_(C.NSControlStateValueOn if runtime in policy.get("runtimes", {}) else C.NSControlStateValueOff)
        self.locality_confirm.setState_(C.NSControlStateValueOn if policy.get("runtime_local_only_confirmed") else C.NSControlStateValueOff)
        self.settings_terms.setString_("\n".join(policy.get("sensitive_terms", [])))
        self._select_popup(self.settings_mode, policy.get("escalation_mode", "flag"))
        self.settings_loaded = True
        self.settings_form_snapshot = self._settings_values()
        self.settings_base = copy.deepcopy(policy)

    @objc.python_method
    def _settings_values(self):
        values = {key: str(field.stringValue()) for key, field in self.settings_fields.items()}
        values.update(runtime_local_only_confirmed=self.locality_confirm.state() == C.NSControlStateValueOn, sensitive_terms=str(self.settings_terms.string()), escalation_mode=self._popup_value(self.settings_mode))
        for runtime in RUNTIME_NAMES:
            values[runtime + "_enabled"] = self.runtime_enabled[runtime].state() == C.NSControlStateValueOn
        return values

    @objc.python_method
    def _save_settings(self):
        values = self._settings_values()
        # Validate locally, then merge these form fields into freshly read policy.
        settings_policy(self.state["policy"], values)
        def save():
            current = self.controller.call("/loqalyn/policy")
            if current != self.settings_base:
                raise ValueError('Saved policy changed while these settings were being edited. Reload settings before saving.')
            policy = settings_policy(current, values)
            return self.controller.call("/loqalyn/policy", "PUT", policy)
        def done(policy):
            self._fill_settings(policy)
            self._status("Settings saved.")
            self._refresh(False, quiet=True)
        self._work("settings", save, done)

    @objc.python_method
    def _reload_settings(self):
        if self._confirm("Reload saved settings?", "Unsaved changes on this Settings page will be discarded.", "Reload"):
            self._work("settings", lambda: self.controller.call("/loqalyn/policy"), self._fill_settings)

    @objc.python_method
    def _save_key(self):
        provider = self._popup_value(self.key_provider)
        secret = str(self.key_field.stringValue()).strip()
        if not secret:
            raise ValueError("Enter an API key first.")
        self.key_field.setStringValue_("")
        self._work("key", lambda: self.controller.set_key(provider, secret), lambda result: self._status("Secret saved in macOS Keychain. No inference request was sent."))

    @objc.python_method
    def _clear_review(self):
        self.review=None; self.review_record=None; self.review_request_id=None
        for editor in (self.review_payload,self.review_outgoing,self.review_response):
            editor.setString_('')
            manager=editor.undoManager()
            if manager: manager.removeAllActions()
        self.review_confirm.setState_(C.NSControlStateValueOff)
        self.review_hash.setStringValue_('Review expired or resolved. Select a pending request.')

    @objc.python_method
    def _escalation_selected(self):
        self._clear_review()
        record = self._selected(self.escalation_table, self.escalation_source)
        self.review = None
        self.review_record = None
        self.review_request_id = None
        self.review_confirm.setState_(C.NSControlStateValueOff)
        self.review_outgoing.setString_("")
        self.review_payload.setString_("")
        self.review_response.setString_("")
        self.review_hash.setStringValue_("Preview before approval. Changes invalidate the preview.")
        self._sync_buttons()
        if not record:
            return
        request_id = record["id"]
        self.review_note.setStringValue_("Loading the selected request…")
        def done(value):
            selected = self._selected(self.escalation_table, self.escalation_source)
            if not selected or selected["id"] != request_id:
                return
            self.review_record = value
            self.review_request_id = request_id
            self.review_payload.setString_(pretty(value["payload"]) if value.get("payload") else "")
            self.review_response.setString_(pretty(value["result"]) if value.get("result") else "")
            self.review_note.setStringValue_(f"Status: {value.get('status', 'unknown')}. " + ("Redact messages, system instructions and tools, then preview." if value.get("status") == "pending" else "This request is no longer pending."))
            self._sync_buttons()
        self._work("detail-" + request_id, lambda: self.controller.call("/loqalyn/escalations/" + quote(request_id, safe="")), done)

    def controlTextDidChange_(self, notification):
        if notification.object() == self.review_model:
            self._invalidate_review()

    def textDidChange_(self, notification):
        if notification.object() == self.review_payload:
            self._invalidate_review()

    @objc.python_method
    def _invalidate_review(self):
        if self.review is not None:
            self.review = None
            self.review_confirm.setState_(C.NSControlStateValueOff)
            self.review_hash.setStringValue_("Preview is out of date. Preview the edited request again before approval.")
            self._sync_buttons()

    @objc.python_method
    def _review_inputs(self):
        record = self._selected(self.escalation_table, self.escalation_source)
        if not record or record["id"] != self.review_request_id or record.get("status") != "pending":
            raise ValueError("Select a pending escalation first.")
        return approval_input(record["id"], self._popup_value(self.review_provider), str(self.review_model.stringValue()), str(self.review_payload.string()))

    @objc.python_method
    def _preview_escalation(self):
        current = self._review_inputs()
        self.review = None
        self.review_confirm.setState_(C.NSControlStateValueOff)
        body = {"action": "preview", **{key: current[key] for key in ("payload", "provider", "model")}}
        def done(result):
            try:
                unchanged = self._review_inputs() == current
            except ValueError:
                unchanged = False
            if not unchanged:
                self._status("Review inputs changed. Preview again.")
                return
            self.review = ApprovalReview(copy.deepcopy(current), copy.deepcopy(result))
            self.review_outgoing.setString_(pretty(result["payload"]))
            self.review_hash.setStringValue_("Provider: " + result["provider"] + "   Model: " + result["model"] + "\nSHA-256: " + result["hash"])
            tags = result.get("tags", [])
            self.review_note.setStringValue_("Approval blocked. Redact these detections and preview again: " + ", ".join(tags) if tags else "Review the exact outgoing payload below. The scanner is a heuristic, not a guarantee that every secret was found.")
            self._sync_buttons()
        self._work("review", lambda: self.controller.call("/loqalyn/escalations/" + quote(current["request_id"], safe=""), "POST", body), done)

    @objc.python_method
    def _approve_escalation(self):
        if not self.review:
            raise ValueError("Preview the exact outgoing payload before approving.")
        current = self._review_inputs()
        body = self.review.approval_body(current, self.review_confirm.state() == C.NSControlStateValueOn)
        self.review_confirm.setState_(C.NSControlStateValueOff)
        self.review = None
        def done(result):
            selected = self._selected(self.escalation_table, self.escalation_source)
            if selected and selected["id"] == current["request_id"]:
                selected["status"] = "approved"
                self.review_response.setString_(pretty(result.get("result", result)))
                self.review_note.setStringValue_("Provider request completed. This approval cannot be reused.")
            else:
                self._status("Provider request completed. Select the approved request to read its response.")
            self._refresh(False, quiet=True)
        self._work("review", lambda: self.controller.call("/loqalyn/escalations/" + quote(current["request_id"], safe=""), "POST", body), done)

    @objc.python_method
    def _deny_escalation(self):
        record = self._selected(self.escalation_table, self.escalation_source)
        if not record or record.get("status") != "pending":
            raise ValueError("Select a pending escalation first.")
        request_id = record["id"]
        self.review = None
        def done(result):
            selected = self._selected(self.escalation_table, self.escalation_source)
            if selected and selected["id"] == request_id:
                selected["status"] = "denied"
                self.review_note.setStringValue_("Kept local. No provider request was sent.")
                self.review_payload.setString_("")
                self.review_outgoing.setString_("")
            self._status("Kept local. No provider request was sent.")
            self._refresh(False, quiet=True)
        self._work("review", lambda: self.controller.call("/loqalyn/escalations/" + quote(request_id, safe=""), "POST", {"action": "deny"}), done)

    @objc.python_method
    def _copy_endpoint(self):
        self._copy(self.controller.endpoint + "/v1")
        self._status("API base URL copied.")

    @objc.python_method
    def _copy_token(self):
        if not self.started:
            raise ValueError("Start the service before copying its API token.")
        self._copy(self.controller.token)
        self._status("API token copied. Paste it only into trusted local clients.")

    @objc.python_method
    def _copy(self, value):
        pasteboard = C.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(str(value), C.NSPasteboardTypeString)

    @objc.python_method
    def _show_data_folder(self):
        path = Path(self.controller.data_dir)
        C.NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(str(path), str(path.parent))

    @objc.python_method
    def _build_claude(self):
        page=self._page('Claude Code','Your coding assistant. Your local models.','Connect Claude Code to Loqalyn in a few steps.',1050)
        self._label(page,'AUTOMATIC LOCAL CODING',24,115,PAGE_WIDTH-48,22,size=11,bold=True,secondary=True)
        self._label(page,'One model per task',24,147,PAGE_WIDTH-48,36,size=26,bold=True)
        self._label(page,'Loqalyn selects a verified local model when a task begins. Every follow-up tool call stays on that model, even after a Loqalyn restart.',24,195,PAGE_WIDTH-48,54,size=14,secondary=True,stretch=True)
        self.claude_summary=self._label(page,'Checking your setup…',24,261,PAGE_WIDTH-48,48,size=13,stretch=True)
        self._button(page,'Check setup',24,321,140,self._check_claude,'claude_check')
        self._button(page,'Choose project…',180,321,170,self._choose_claude_project,'claude_project')
        self._button(page,'Open Claude Code',366,321,190,self._launch_claude,'claude_launch')
        self._button(page,'Find models',572,321,140,lambda:self._navigate('Discover'),'claude_find')
        self.controls['claude_launch'].tone='primary'
        self.claude_project_label=self._label(page,'Choose the folder you want to work on.',24,370,PAGE_WIDTH-48,42,secondary=True,stretch=True)
        self._label(page,'Available coding models',24,431,PAGE_WIDTH-48,28,size=17,bold=True)
        self.claude_models,self.claude_models_source=self._table(page,[('name','Model',270,lambda r:r['name']),('status','Readiness',450,lambda r:'Ready · '+str(r['memory_gb'])+' GiB reserved' if r['ready'] else ' · '.join(r['reasons']))],475,150,self._sync_buttons)
        self._button(page,'Check tool support',24,643,190,self._verify_claude_model,'claude_verify')
        self.claude_check_note=self._label(page,'The check runs two short local requests. It does not execute commands or modify project files.',232,645,PAGE_WIDTH-256,49,size=11,secondary=True,stretch=True)
        self._label(page,'Recent tasks',24,719,PAGE_WIDTH-48,28,size=17,bold=True)
        self.claude_tasks,self.claude_tasks_source=self._table(page,[('task','Task',135,lambda r:r['task']),('model','Locked model',360,lambda r:r['model']),('requests','Requests',90,lambda r:r['requests']),('mode','Lock',90,lambda r:r['mode'])],760,130,lambda:None)
        self._label(page,'Text coding with Ollama · Claude Code keeps its permission prompts. A separate profile leaves your existing settings intact. Model inference stays local; approved terminal commands can still access the network.',24,913,PAGE_WIDTH-48,60,size=11,secondary=True,stretch=True)
        self._button(page,'Reset coding connection…',24,987,220,self._reset_claude,'claude_reset')

    @objc.python_method
    def _check_claude(self):
        from .claude import client_info
        def done(info):
            self.claude_client=info; self._refresh(discover=True,quiet=True); self._update_claude(); self._sync_buttons(); self._status(info['message'])
        self._work('claude-check',client_info,done)

    @objc.python_method
    def _update_claude(self):
        coding=(self.state or {}).get('claude_code',{})
        ready=sum(m['ready'] for m in coding.get('models',[]))
        self.claude_summary.setStringValue_(self.claude_client['message']+'  '+(f'{ready} local coding model'+('s' if ready!=1 else '')+' ready.' if ready else 'Select a compatible model below and check its tool support.'))
        self._reload_table(self.claude_models,self.claude_models_source,coding.get('models',[]))
        self._reload_table(self.claude_tasks,self.claude_tasks_source,coding.get('tasks',[]))

    @objc.python_method
    def _choose_claude_project(self):
        panel=C.NSOpenPanel.openPanel(); panel.setCanChooseDirectories_(True); panel.setCanChooseFiles_(False); panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_('Choose project')
        if panel.runModal()==C.NSModalResponseOK:
            self.claude_project=Path(str(panel.URL().path())).resolve()
            self.claude_project_label.setStringValue_(str(self.claude_project)); self._check_claude(); self._sync_buttons()

    @objc.python_method
    def _verify_claude_model(self):
        model=self._selected(self.claude_models,self.claude_models_source)
        if not model: return
        self.claude_check_note.setStringValue_('Checking the model’s tool call and tool-result handling locally…')
        def done(result):
            self.claude_check_note.setStringValue_('Tool check passed. Coding quality depends on the model and task.'); self._refresh(discover=True)
        self._work('claude-verify',lambda:self.controller.call('/loqalyn/claude/verify','POST',{'model':model['id']}),done)

    @objc.python_method
    def _launch_claude(self):
        from .claude import write_launcher
        resources=os.environ.get('LOQALYN_RESOURCES')
        if not resources: raise ValueError('Open the installed Loqalyn app to launch Terminal. From source, use loqalyn claude PROJECT.')
        executable=Path(resources).parent/'MacOS'/'Loqalyn'
        project=self.claude_project
        def done(path):
            opened=C.NSWorkspace.sharedWorkspace().openFile_withApplication_(str(path),'Terminal')
            if not opened: self._error('Terminal could not open. Use the Loqalyn command-line launcher instead.')
            else: self._status('Claude Code opened in Terminal. Keep Loqalyn running while you work.')
        self._work('claude-launch',lambda:write_launcher(self.controller.data_dir,self.controller.port,project,executable),done)

    @objc.python_method
    def _reset_claude(self):
        if self._confirm('Reset the coding connection?','This revokes the coding credential and clears saved task locks. Close existing Claude Code sessions, then open a new one from Loqalyn. Your conversations and project files remain available.','Reset'):
            self._work('claude-reset',lambda:self.controller.call('/loqalyn/claude/reset','POST',{'confirmed':True}),lambda result:self._refresh())

    @objc.python_method
    def _save_text(self, name, text):
        panel = C.NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(name)
        panel.setCanCreateDirectories_(True)
        if panel.runModal() == C.NSModalResponseOK:
            path = Path(str(panel.URL().path()))
            self._work("export", lambda: path.write_text(text, encoding="utf-8"), lambda result: self._status("Export saved: " + path.name))

    @objc.python_method
    def _export_policy(self):
        self._save_text("loqalyn-policy.yaml", str(self.policy_editor.string()))

    @objc.python_method
    def _export_activity(self):
        self._save_text("loqalyn-decisions.json", pretty((self.state or {}).get("decisions", [])))


_APP_DELEGATE = None


def run_app(controller):
    """Create the native application and run Cocoa's main-thread event loop."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("The macOS interface must start on the main thread.")
    F.NSProcessInfo.processInfo().setProcessName_("Loqalyn")
    application = C.NSApplication.sharedApplication()
    application.setActivationPolicy_(C.NSApplicationActivationPolicyRegular)
    resources = os.environ.get("LOQALYN_RESOURCES")
    if resources:
        icon = C.NSImage.alloc().initWithContentsOfFile_(str(Path(resources) / "Loqalyn.icns"))
        if icon is not None:
            application.setApplicationIconImage_(icon)
    global _APP_DELEGATE
    _APP_DELEGATE = DesktopApplication.alloc().init().configure(controller)
    # NSApplication's delegate property does not retain its delegate.
    application.setDelegate_(_APP_DELEGATE)
    AppHelper.runEventLoop()


run = run_app
