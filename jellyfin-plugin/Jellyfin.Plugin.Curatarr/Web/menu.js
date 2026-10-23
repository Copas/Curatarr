// Curatarr: a "Leaving Soon" item in Jellyfin's web menus, beside Delete.
// Added to the web app's page by the Curatarr plugin (through the File
// Transformation plugin). Delete in the web app then deletes straight away.
(function () {
    'use strict';
    var ID = /^[0-9a-f]{32}$/;
    var last = { id: null, at: 0 };

    function detailsId() {
        var match = /[?&]id=([0-9a-f]{32})/.exec(window.location.hash || window.location.search);
        return match ? match[1] : null;
    }

    // Remember which item a menu is about to open for: a card or list row,
    // or the title page's own "more" button.
    function remember(event) {
        var target = event.target;
        if (!(target instanceof Element) || target.closest('.actionSheet')) {
            return;
        }
        var row = target.closest('[data-id]');
        var id = row && ID.test(row.getAttribute('data-id')) ? row.getAttribute('data-id') : null;
        if (!id && target.closest('.btnMoreCommands, .detailButton, .mainDetailButtons')) {
            id = detailsId();
        }
        if (id) {
            last = { id: id, at: Date.now() };
        }
    }

    ['pointerdown', 'click', 'contextmenu', 'keydown'].forEach(function (name) {
        document.addEventListener(name, remember, true);
    });

    function toast(text) {
        var box = document.createElement('div');
        box.textContent = text;
        box.setAttribute('role', 'status');
        box.style.cssText = 'position:fixed;left:50%;bottom:5em;transform:translateX(-50%);z-index:100000;'
            + 'background:#202020;color:#fff;padding:.8em 1.2em;border-radius:.4em;'
            + 'box-shadow:0 .2em .8em rgba(0,0,0,.5);max-width:90vw;font-size:1em';
        document.body.appendChild(box);
        setTimeout(function () { box.remove(); }, 5000);
    }

    function api(method, path) {
        var client = window.ApiClient;
        if (!client) {
            return Promise.reject(new Error('no ApiClient'));
        }
        return client.ajax({ type: method, url: client.getUrl(path), dataType: 'json' });
    }

    function addTo(sheet) {
        var del = sheet.querySelector('button.actionSheetMenuItem[data-id="delete"]');
        if (!del || sheet.querySelector('[data-id="curatarr-leaving-soon"]')) {
            return;
        }
        var id = Date.now() - last.at < 10000 ? last.id : detailsId();
        if (!id) {
            return;
        }
        api('GET', 'Curatarr/Items/' + id).then(function (status) {
            if (!status || !status.canLeave || !del.isConnected) {
                return;
            }
            var item = del.cloneNode(true);
            item.setAttribute('data-id', 'curatarr-leaving-soon');
            var icon = item.querySelector('.actionsheetMenuItemIcon');
            if (icon) {
                icon.classList.remove('delete');
                icon.classList.add('auto_delete');
            }
            var text = item.querySelector('.actionSheetItemText');
            if (text) {
                text.textContent = 'Leaving Soon';
            }
            // The sheet closes itself on any click; this runs first.
            item.addEventListener('click', function () {
                api('POST', 'Curatarr/Items/' + id + '/LeavingSoon').then(function (answer) {
                    toast((answer && answer.message) || 'Marked Leaving Soon.');
                }, function () {
                    toast("Curatarr couldn't mark it Leaving Soon.");
                });
            });
            del.parentNode.insertBefore(item, del);
        }, function () { /* Curatarr or the plugin unreachable: leave the menu as it is */ });
    }

    new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
            mutation.addedNodes.forEach(function (node) {
                if (!(node instanceof Element)) {
                    return;
                }
                if (node.matches('.actionSheet')) {
                    addTo(node);
                }
                node.querySelectorAll('.actionSheet').forEach(addTo);
            });
        });
    }).observe(document.body, { childList: true, subtree: true });
})();
