(function ($) {
    function normalizeFunctionUrl(url, functionName) {
        if (!url) {
            return url;
        }

        return url
            .replace(/\/webapp\/api\//, "/api/")
            .replace(
                new RegExp(`/api/${functionName}$`),
                `/2015-03-31/functions/${functionName}/invocations`
            )
            .replace(
                new RegExp(`/webapp/2015-03-31/functions/${functionName}/invocations$`),
                `/2015-03-31/functions/${functionName}/invocations`
            );
    }

    function getBaseUrl() {
        const currentUrl = new URL(document.location.href);
        if (currentUrl.protocol === "file:") {
            return `http://localhost:4566`;
        }

        const origin = `${currentUrl.protocol}//${currentUrl.host}`
            .replace("://webapp.s3.", "://")
            .replace("://webapp.s3-website.", "://");

        // Local MiniStack serves Lambda invocations at the root, not under /webapp.
        if (currentUrl.host === "localhost:4566" || currentUrl.host === "127.0.0.1:4566") {
            return origin;
        }

        const pathPrefix = currentUrl.pathname.replace(/\/[^/]*$/, "");
        return `${origin}${pathPrefix}`;
    }

    function getInvokeUrl(functionName) {
        const baseUrl = getBaseUrl();
        return normalizeFunctionUrl(
            `${baseUrl.replace(/\/webapp$/, "")}/2015-03-31/functions/${functionName}/invocations`,
            functionName
        );
    }

    let functionUrlPresign = localStorage.getItem("functionUrlPresign");
    if (functionUrlPresign) {
        functionUrlPresign = normalizeFunctionUrl(functionUrlPresign, "presign");
        $("#functionUrlPresign").val(functionUrlPresign);
        localStorage.setItem("functionUrlPresign", functionUrlPresign);
    }

    let functionUrlList = localStorage.getItem("functionUrlList");
    if (functionUrlList) {
        functionUrlList = normalizeFunctionUrl(functionUrlList, "list");
        console.log("function url list is", functionUrlList);
        $("#functionUrlList").val(functionUrlList);
        localStorage.setItem("functionUrlList", functionUrlList);
    }

    let imageItemTemplate = Handlebars.compile($("#image-item-template").html());

    $("#configForm").submit(async function (event) {
        if (event.preventDefault)
            event.preventDefault();
        else
            event.returnValue = false;

        event.preventDefault();
        let action = $(this).find("button[type=submit]:focus").attr('name');
        if (action === undefined) {
            // the jquery find with the focus does not work on Safari, maybe because the focus is not instantly given
            // fallback to manually retrieving the submitter from the original event
            action = event.originalEvent.submitter.getAttribute('name')
        }

        if (action == "load") {
            const presignUrl = getInvokeUrl("presign");
            const listUrl = getInvokeUrl("list");
            $("#functionUrlPresign").val(presignUrl);
            $("#functionUrlList").val(listUrl);
            localStorage.setItem("functionUrlPresign", presignUrl);
            localStorage.setItem("functionUrlList", listUrl);
            // alert("Local MiniStack invoke URLs loaded");
        } else if (action == "save") {
            localStorage.setItem("functionUrlPresign", $("#functionUrlPresign").val());
            localStorage.setItem("functionUrlList", $("#functionUrlList").val());
            // alert("Configuration saved");
        } else if (action == "clear") {
            localStorage.removeItem("functionUrlPresign");
            localStorage.removeItem("functionUrlList");
            $("#functionUrlPresign").val("")
            $("#functionUrlList").val("")
            // alert("Configuration cleared");
        } else {
            alert("Unknown action");
        }

    });

    $("#uploadForm").submit(function (event) {
        $("#uploadForm button").addClass('disabled');

        if (event.preventDefault)
            event.preventDefault();
        else
            event.returnValue = false;

        event.preventDefault();

        let fileName = $("#customFile").val().replace(/C:\\fakepath\\/i, '');
        let functionUrlPresign = $("#functionUrlPresign").val();

        // modify the original form
        console.log(fileName, functionUrlPresign);

        let urlToCall = functionUrlPresign
        console.log(urlToCall);

        $.ajax({
            type: "POST",
            url: urlToCall,
            data: JSON.stringify({key: fileName}),
            contentType: "application/json",
            success: function (data) {
                console.log("got pre-signed POST URL", data);

                let fields = data['fields'];

                let formData = new FormData()
                
                Object.entries(fields).forEach(([field, value]) => {
                    formData.append(field, value);
                });

                // the file <input> element, "file" needs to be the last element of the form
                const fileElement = document.querySelector("#customFile");
                formData.append("file", fileElement.files[0]);

                console.log("sending form data", formData);

                $.ajax({
                    type: "POST",
                    url: data['url'],
                    data: formData,
                    processData: false,
                    contentType: false,
                    success: function () {
                        // alert("success!");
                        updateImageList();
                    },
                    error: function (jqXHR, textStatus, errorThrown) {
                        console.error("Upload POST failed", {
                            presignUrl: functionUrlPresign,
                            uploadUrl: data['url'],
                            status: jqXHR.status,
                            statusText: jqXHR.statusText,
                            textStatus: textStatus,
                            errorThrown: errorThrown,
                            responseText: jqXHR.responseText,
                            responseHeaders: jqXHR.getAllResponseHeaders(),
                        });
                        alert("upload failed! check the logs");
                    },
                    complete: function (event) {
                        console.log("done", event);
                        $("#uploadForm button").removeClass('disabled');
                    }
                });
            },
            error: function (e) {
                console.error("Presign request failed", {
                    presignUrl: functionUrlPresign,
                    status: e.status,
                    statusText: e.statusText,
                    responseText: e.responseText,
                    responseHeaders: e.getAllResponseHeaders ? e.getAllResponseHeaders() : undefined,
                });
                alert("error getting pre-signed URL. check the logs!");
                $("#uploadForm button").removeClass('disabled');
            }
        });
    });

    function updateImageList() {
        let listUrl = $("#functionUrlList").val();
        if (!listUrl) {
            alert("Please set the function URL of the list Lambda");
            return
        }

        $.ajax({
            type: "POST",
            url: listUrl,
            data: JSON.stringify({}),
            contentType: "application/json",
            success: function (response) {
                if (typeof response === "string") {
                    response = JSON.parse(response);
                }
                $('#imagesContainer').empty(); // Empty imagesContainer
                response.forEach(function (item) {
                    console.log(item);
                    let cardHtml = imageItemTemplate(item);
                    $("#imagesContainer").append(cardHtml);
                });
            },
            error: function (jqXHR, textStatus, errorThrown) {
                console.log("Error:", textStatus, errorThrown);
                alert("error! check the logs");
            }
        });
    }

    $("#updateImageListButton").click(function (event) {
        updateImageList();
    });

    if (functionUrlList) {
        updateImageList();
    }

})(jQuery);
