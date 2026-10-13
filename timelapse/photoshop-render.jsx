/* Photoshop's own Camera Raw renderer. Paths arrive as data via Apple events. */
(function (args) {
    var mode = args[0];
    if (mode === "check") return "TIMELAPSE_READY|" + app.version;
    var input = new File(args[1]), output = new File(args[2]);
    var edge = Number(args[3]), lease = new File(args[4]), nonce = args[5];
    var deadline = Number(args[6]);
    function authorised() {
        if (!lease.exists || !lease.open("r")) return false;
        var value = lease.read(); lease.close();
        return value === nonce;
    }
    // A queued Apple event must not begin rendering after a timeout/cancellation.
    if (new Date().getTime() > deadline || !authorised()) throw Error("TIMELAPSE_EXPIRED");
    if (!input.exists || output.exists) throw Error("Input missing or output already exists");
    var dialogs = app.displayDialogs, previous = null, doc = null, ids = {};
    for (var i = 0; i < app.documents.length; i++) ids[app.documents[i].id] = true;
    if (app.documents.length) previous = app.activeDocument;
    try {
        app.displayDialogs = DialogModes.NO;
        var opened = app.open(input);
        if (ids[opened.id]) throw Error("Refusing to change an already open document");
        doc = opened;
        if (!authorised()) throw Error("TIMELAPSE_CANCELLED");
        if (doc.mode !== DocumentMode.RGB) doc.changeMode(ChangeMode.RGB);
        doc.convertProfile("sRGB IEC61966-2.1", Intent.RELATIVECOLORIMETRIC, true, true);
        doc.bitsPerChannel = BitsPerChannelType.EIGHT;
        var width = doc.width.as("px"), height = doc.height.as("px");
        if (edge > 0 && Math.max(width, height) > edge) {
            var ratio = edge / Math.max(width, height);
            doc.resizeImage(UnitValue(Math.round(width * ratio), "px"),
                            UnitValue(Math.round(height * ratio), "px"), null, ResampleMethod.BICUBIC);
        }
        if (!authorised()) throw Error("TIMELAPSE_CANCELLED");
        var options = new JPEGSaveOptions();
        options.quality = 12;
        options.embedColorProfile = true;
        options.formatOptions = FormatOptions.STANDARDBASELINE;
        // Save Photoshop's actual rendered metadata. Never replace it with the
        // requested XMP to make a provenance comparison appear to pass.
        doc.saveAs(output, options, true, Extension.LOWERCASE);
        return "TIMELAPSE_RENDERED|" + Math.round(width) + "|" + Math.round(height) + "|" + Math.round(doc.width.as("px")) + "|" + Math.round(doc.height.as("px"));
    } finally {
        if (doc) doc.close(SaveOptions.DONOTSAVECHANGES);
        app.displayDialogs = dialogs;
        if (previous) {
            try { app.activeDocument = previous; } catch (_) {}
        }
    }
})(arguments);
